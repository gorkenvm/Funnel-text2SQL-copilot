"""Tests for the module M13 demo gate (app.main._demo_gate_middleware).

DEMO_PASSPHRASE is read fresh from the environment on every request (see
app.main._demo_passphrase's docstring), so these tests turn the gate on
purely via monkeypatch.setenv — no other test file/conftest.py sets it,
which is exactly what keeps the whole rest of the suite (gate OFF) byte
for byte unaffected. A TEST-ONLY passphrase is used throughout; the real
one lives only in the operator's gitignored .env and never appears here.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.main import app

client = TestClient(app)

TEST_PASSPHRASE = "open sesame demo"


@pytest.fixture(autouse=True)
def _reset_gate_state(monkeypatch):
    # Every test starts with the gate off and a clean per-IP failure/lockout
    # table, regardless of what an earlier test in this module did.
    monkeypatch.delenv("DEMO_PASSPHRASE", raising=False)
    main._demo_gate_limiter._failures.clear()
    main._demo_gate_limiter._locked_until.clear()


def _set_passphrase(monkeypatch, value=TEST_PASSPHRASE):
    monkeypatch.setenv("DEMO_PASSPHRASE", value)


class TestGateOffIsUnchangedBehavior:
    def test_no_header_needed_when_unset(self):
        resp = client.get("/api/metrics")
        assert resp.status_code == 200

    def test_health_reports_locked_false(self):
        resp = client.get("/health")
        assert resp.json()["locked"] is False


class TestNormalizedKeyMatching:
    def test_exact_key_passes(self, monkeypatch):
        _set_passphrase(monkeypatch)
        resp = client.get("/api/metrics", headers={"X-Demo-Key": TEST_PASSPHRASE})
        assert resp.status_code == 200

    def test_case_insensitive(self, monkeypatch):
        _set_passphrase(monkeypatch)
        resp = client.get("/api/metrics", headers={"X-Demo-Key": TEST_PASSPHRASE.upper()})
        assert resp.status_code == 200

    def test_extra_and_collapsed_whitespace_still_matches(self, monkeypatch):
        _set_passphrase(monkeypatch)
        resp = client.get(
            "/api/metrics", headers={"X-Demo-Key": "  open   sesame  DEMO   "}
        )
        assert resp.status_code == 200

    def test_wrong_key_is_rejected(self, monkeypatch):
        _set_passphrase(monkeypatch)
        resp = client.get("/api/metrics", headers={"X-Demo-Key": "not it"})
        assert resp.status_code == 401

    def test_missing_key_is_rejected(self, monkeypatch):
        _set_passphrase(monkeypatch)
        resp = client.get("/api/metrics")
        assert resp.status_code == 401


class TestStatic401Shape:
    def test_wrong_key_returns_the_static_body(self, monkeypatch):
        _set_passphrase(monkeypatch)
        resp = client.get("/api/metrics", headers={"X-Demo-Key": "nope"})
        assert resp.status_code == 401
        assert resp.json() == {
            "error": "locked",
            "message": "This is a private demo. Enter the passphrase on the page to continue.",
        }

    def test_missing_key_returns_the_same_static_body(self, monkeypatch):
        _set_passphrase(monkeypatch)
        resp = client.get("/api/metrics")
        assert resp.json() == {
            "error": "locked",
            "message": "This is a private demo. Enter the passphrase on the page to continue.",
        }

    def test_every_gated_endpoint_is_covered(self, monkeypatch):
        _set_passphrase(monkeypatch)
        assert client.post("/api/ask", json={"question": "x"}).status_code == 401
        assert client.get("/api/ask/stream", params={"q": "x"}).status_code == 401
        assert client.get("/api/metrics").status_code == 401
        assert client.post("/api/dashboard", json={}).status_code == 401
        assert client.get("/api/catalog").status_code == 401

    def test_health_and_static_root_stay_open_even_when_locked(self, monkeypatch):
        _set_passphrase(monkeypatch)
        assert client.get("/health").status_code == 200
        assert client.get("/").status_code == 200


class TestStreamEndpointNeverStartsOnAFailedKey:
    def test_wrong_key_is_a_clean_json_401_not_an_event_stream(self, monkeypatch):
        _set_passphrase(monkeypatch)
        resp = client.get("/api/ask/stream", params={"q": "anything"})
        assert resp.status_code == 401
        assert "text/event-stream" not in resp.headers.get("content-type", "")
        assert resp.json()["error"] == "locked"

    def test_query_param_fallback_k_is_accepted_only_here(self, monkeypatch):
        _set_passphrase(monkeypatch)
        resp = client.get(
            "/api/ask/stream", params={"q": "anything", "k": TEST_PASSPHRASE}
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

    def test_query_param_k_is_not_honored_on_other_endpoints(self, monkeypatch):
        _set_passphrase(monkeypatch)
        resp = client.get("/api/metrics", params={"k": TEST_PASSPHRASE})
        assert resp.status_code == 401


class TestLlmDriverAgentNeverTouchedOnAFailedKey:
    def test_zero_calls_on_wrong_key(self, monkeypatch):
        _set_passphrase(monkeypatch)
        ask_mock = Mock()
        query_mock = Mock()
        plan_mock = Mock()
        monkeypatch.setattr(main.agent, "ask", ask_mock)
        monkeypatch.setattr(main.driver, "query", query_mock)
        monkeypatch.setattr(main.llm, "plan", plan_mock)

        resp = client.post("/api/ask", json={"question": "Where is the drop-off?"})

        assert resp.status_code == 401
        ask_mock.assert_not_called()
        query_mock.assert_not_called()
        plan_mock.assert_not_called()

    def test_zero_calls_on_missing_key_for_dashboard(self, monkeypatch):
        _set_passphrase(monkeypatch)
        query_mock = Mock()
        monkeypatch.setattr(main.driver, "query", query_mock)

        resp = client.post("/api/dashboard", json={})

        assert resp.status_code == 401
        query_mock.assert_not_called()


class TestPerIpBruteForceDamping:
    def test_locks_out_after_five_consecutive_failures(self, monkeypatch):
        _set_passphrase(monkeypatch)
        for _ in range(5):
            resp = client.get("/api/metrics", headers={"X-Demo-Key": "wrong"})
            assert resp.status_code == 401

        locked_resp = client.get("/api/metrics", headers={"X-Demo-Key": "wrong"})
        assert locked_resp.status_code == 429

        # Even the CORRECT key is refused while the IP is locked out — the
        # lockout is time-based, not "still guessing wrong".
        still_locked = client.get("/api/metrics", headers={"X-Demo-Key": TEST_PASSPHRASE})
        assert still_locked.status_code == 429

    def test_a_success_resets_the_failure_counter(self, monkeypatch):
        _set_passphrase(monkeypatch)
        for _ in range(4):
            assert client.get("/api/metrics", headers={"X-Demo-Key": "wrong"}).status_code == 401
        # One success short of the lockout threshold, then a correct key —
        # the next 4 wrong attempts must NOT immediately lock out.
        assert client.get("/api/metrics", headers={"X-Demo-Key": TEST_PASSPHRASE}).status_code == 200
        for _ in range(4):
            assert client.get("/api/metrics", headers={"X-Demo-Key": "wrong"}).status_code == 401
        assert client.get("/api/metrics", headers={"X-Demo-Key": TEST_PASSPHRASE}).status_code == 200


class TestHealthLockFlag:
    def test_locked_true_when_passphrase_set(self, monkeypatch):
        _set_passphrase(monkeypatch)
        assert client.get("/health").json()["locked"] is True

    def test_locked_false_when_unset(self):
        assert client.get("/health").json()["locked"] is False

    def test_passphrase_itself_never_appears_in_the_health_payload(self, monkeypatch):
        _set_passphrase(monkeypatch)
        body = client.get("/health").json()
        assert TEST_PASSPHRASE not in str(body)
