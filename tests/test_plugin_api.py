"""Tests for the hermes-quota backend: OQG mapping, graceful degradation, endpoints.

Run with the Hermes environment (FastAPI lives there):
  /Users/shift/.hermes/hermes-agent/.venv/bin/python -m unittest discover \
      -s /Users/shift/.hermes/plugins/hermes-quota/tests -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

PLUGIN_DIR = Path(__file__).resolve().parents[1]
API_FILE = PLUGIN_DIR / "dashboard" / "plugin_api.py"
SPEC = importlib.util.spec_from_file_location("hermes_quota_plugin_api", str(API_FILE))
api = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = api
SPEC.loader.exec_module(api)

PREFIX = "/api/plugins/hermes-quota"


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    """Loopback transport stand-in: path -> JSON object (or None = refused)."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append({
            "url": request.full_url,
            "timeout": timeout,
            "headers": dict(request.headers),
        })
        path = urllib.parse.urlsplit(request.full_url).path
        payload = self.responses.get(path)
        if payload is None:
            raise urllib.error.URLError("connection refused (simulated)")
        return FakeResponse(json.dumps(payload).encode("utf-8"))


def make_quota_payload() -> dict:
    return {
        "generatedAtMs": 1760000000000,
        "stale": False,
        "timeZone": "America/Santiago",
        "providers": {
            "cards": [
                {
                    "cardId": "card-abc",
                    "providerId": "opencode-go",
                    "providerLabel": "OpenCode Zen",
                    "planLabel": "Go",
                    "accountLabel": "cuenta-real",
                    "labelSource": "account",
                    "stale": False,
                    "rows": [
                        {
                            "rowId": "card-abc#window:five_hour",
                            "label": "5h",
                            "kind": "five_hour",
                            "resultType": "quota",
                            "percentUsed": 37.5,
                            "percentRemaining": 62.5,
                            "resetAtIso": "2026-11-22T20:00:00Z",
                        },
                        {
                            "rowId": "card-abc#named:credits",
                            "label": "Créditos",
                            "kind": "named",
                            "resultType": "quota",
                            "quantity": {"decimal": "12.50", "unit": {"kind": "count", "unit": "credit"}},
                        },
                    ],
                    "errors": [],
                },
                {
                    "cardId": "card-def",
                    "providerId": "anthropic",
                    "providerLabel": "Anthropic",
                    "stale": True,
                    "rows": [],
                    "errors": [{"label": "auth", "message": "token expirado"}],
                },
            ],
            "enabledProviders": ["opencode-go", "anthropic"],
            "polledAtMs": 1760000000000,
        },
    }


def make_home_payload() -> dict:
    def tokens(total, input_, output, cache_read, reasoning):
        return {
            "total": total, "input": input_, "output": output, "cacheRead": cache_read,
            "cacheWrite": 0, "reasoning": reasoning, "apiEquivalentCostUsd": 0.1234,
            "unpricedTokens": 0, "unknownTokens": 0,
        }

    return {
        "generatedAtMs": 1760000000000,
        "range": "7d",
        "stale": False,
        "usage": {
            "range": "7d",
            "totals": tokens(310, 100, 50, 100, 60),
            "buckets": [
                {"startMs": 1759900000000, "endMs": 1759903600000, "tokens": tokens(150, 50, 20, 50, 30)},
                {"startMs": 1759920000000, "endMs": 1759923600000, "tokens": tokens(160, 50, 30, 50, 30)},
            ],
            "sessionCount": 3,
        },
        "projects": [
            {
                "projectId": "/work/proj", "name": "proj", "tokens": tokens(310, 100, 50, 100, 60),
                "rootSessionCount": 3, "latestActivityAtMs": 1759920000000,
            }
        ],
    }


def make_models_payload() -> dict:
    def tokens(total):
        return {
            "total": total, "input": total, "output": 0, "cacheRead": 0, "cacheWrite": 0,
            "reasoning": 0, "apiEquivalentCostUsd": 0.05, "unpricedTokens": 0, "unknownTokens": 0,
        }

    return {
        "dimension": "model",
        "range": "7d",
        "window": {"untilMs": 1760000000000},
        "bucketUnit": "day",
        "timeZone": "UTC",
        "buckets": [],
        "totals": tokens(9),
        "sessionCount": 2,
        "stepCount": 4,
        "rows": [
            {
                "identity": {"kind": "known", "id": "model-x"},
                "tokens": tokens(9), "sessionCount": 2, "stepCount": 4,
                "children": [{"identity": {"kind": "known", "id": "provider-x"}, "tokens": tokens(9), "sessionCount": 2, "stepCount": 4, "children": []}],
            }
        ],
        "series": [],
        "stale": False,
    }


def full_responses(version="1.2.3"):
    return {
        "/api/v1/health": {"app": "opencode-quota-gui", "version": version, "contractVersion": 2},
        "/api/v1/quota": make_quota_payload(),
        "/api/v1/home": make_home_payload(),
        "/api/v1/models": make_models_payload(),
    }


class QuotaBackendTests(unittest.TestCase):
    def setUp(self):
        api.invalidate_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.db = self.home / "state.db"
        self._create_db()
        self.env_patcher = patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        (self.home / "auth.json").write_text(
            json.dumps({"providers": {"x": {"access_token": "SECRET_TEST_TOKEN_DO_NOT_LEAK"}}})
        )
        app = FastAPI()
        app.include_router(api.router, prefix=PREFIX)
        self.client = TestClient(app)

    def _create_db(self):
        conn = sqlite3.connect(str(self.db))
        try:
            conn.executescript(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, source TEXT, started_at REAL, cwd TEXT,
                    git_repo_root TEXT, display_name TEXT, title TEXT,
                    billing_provider TEXT, last_activity_at REAL
                );
                CREATE TABLE session_model_usage (
                    session_id TEXT NOT NULL, model TEXT NOT NULL,
                    billing_provider TEXT NOT NULL DEFAULT '', last_seen REAL,
                    api_call_count INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0,
                    cache_write_tokens INTEGER DEFAULT 0, reasoning_tokens INTEGER DEFAULT 0
                );
                """
            )
            now = time.time()
            conn.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                ("s1", "desktop", now - 3600, "/work/alpha", "/work/alpha", "Alpha", "Alpha", "provider-a", now - 3600),
            )
            conn.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                ("s2", "desktop", now - 40 * 86400, "/work/old", "/work/old", "Old", "Old", "provider-a", now - 40 * 86400),
            )
            # recent: 100+20+30+10+5 = 165
            conn.execute(
                "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("s1", "model-a", "provider-a", now - 3600, 3, 100, 20, 30, 10, 5),
            )
            # recent: 50+10 = 60
            conn.execute(
                "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("s1", "model-b", "provider-b", now - 1800, 1, 50, 10, 0, 0, 0),
            )
            # outside the 7d window (must appear in lifetime only): 1000
            conn.execute(
                "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("s2", "model-a", "provider-a", now - 40 * 86400, 5, 1000, 0, 0, 0, 0),
            )
            conn.commit()
        finally:
            conn.close()

    def test_opencode_full_mapping(self):
        opener = FakeOpener(full_responses())
        snap = api.build_snapshot("7d", force=True, opener=opener, db_path=self.db)
        self.assertEqual(snap["source"], "opencode")
        self.assertTrue(snap["opencode"]["connected"])
        self.assertEqual(snap["opencode"]["version"], "1.2.3")
        self.assertNotIn("error", snap["opencode"])
        self.assertEqual(snap["notes"], [])

        cards = snap["quota"]["cards"]
        self.assertEqual(len(cards), 2)
        win = cards[0]["windows"][0]
        self.assertEqual(cards[0]["id"], "card-abc")
        self.assertEqual(cards[0]["provider"], "opencode-go")
        self.assertEqual(cards[0]["account"], "cuenta-real")
        self.assertEqual(cards[0]["plan"], "Go")
        self.assertTrue(cards[0]["active"])
        self.assertEqual(win["used"], 37.5)
        self.assertEqual(win["remainingPct"], 62.5)
        self.assertEqual(win["limit"], 100)
        self.assertEqual(win["unit"], "percent")
        self.assertEqual(win["resetAt"], "2026-11-22T20:00:00Z")
        self.assertEqual(cards[0]["windows"][1]["unit"], "credit")
        self.assertFalse(cards[1]["active"])
        self.assertIn("token expirado", cards[1]["error"])

        analytics = snap["analytics"]
        self.assertEqual(analytics["range"], "7d")
        self.assertEqual(analytics["totalTokens"], 310)
        self.assertEqual(analytics["cacheRead"], 100)
        self.assertEqual(analytics["input"], 100)
        self.assertEqual(analytics["reasoning"], 60)
        self.assertEqual(analytics["output"], 50)
        self.assertEqual(analytics["apiEquivalent"], 0.1234)
        self.assertEqual(analytics["daily"], [{"date": "2025-10-08", "tokens": 310}])
        self.assertEqual(analytics["projects"][0]["id"], "/work/proj")
        self.assertEqual(analytics["projects"][0]["sessions"], 3)
        self.assertEqual(snap["models"][0]["model"], "model-x")
        self.assertEqual(snap["models"][0]["provider"], "provider-x")
        self.assertEqual(snap["models"][0]["apiEquivalent"], 0.05)
        for call in opener.calls:
            self.assertLessEqual(call["timeout"], 2.0)
            self.assertNotIn("Authorization", call["headers"])

    def test_degradation_uses_real_db_and_no_credentials(self):
        opener = FakeOpener({})  # everything refused
        snap = api.build_snapshot("7d", force=True, opener=opener, db_path=self.db)
        self.assertEqual(snap["source"], "hermes")
        self.assertFalse(snap["opencode"]["connected"])
        self.assertIn("URLError", snap["opencode"]["error"])
        self.assertTrue(any("No quota cards" in note for note in snap["notes"]))
        self.assertTrue(any(note.startswith("Usage/analytics") for note in snap["notes"]))
        cards = snap["quota"]["cards"]
        self.assertEqual([c["provider"] for c in cards], ["provider-a", "provider-b"])
        card = cards[0]
        self.assertEqual(card["windows"][0]["status"], "no quota source")
        self.assertNotIn("used", card["windows"][0])
        self.assertNotIn("limit", card["windows"][0])
        self.assertFalse(card["active"])
        analytics = snap["analytics"]
        self.assertEqual(analytics["totalTokens"], 225)  # 165 + 60 real tokens
        self.assertEqual(analytics["input"], 150)
        self.assertEqual(analytics["output"], 30)
        self.assertEqual(analytics["cacheRead"], 30)
        self.assertEqual(analytics["reasoning"], 5)
        self.assertEqual(analytics["projects"], [{"id": "/work/alpha", "label": "alpha", "tokens": 225, "sessions": 1}])
        self.assertEqual([m["model"] for m in snap["models"]], ["model-a", "model-b"])
        self.assertEqual(snap["models"][0]["provider"], "provider-a")
        self.assertNotIn("apiEquivalent", snap["models"][0])
        self.assertNotIn("SECRET_TEST_TOKEN_DO_NOT_LEAK", json.dumps(snap))
        self.assertNotIn("apiEquivalent", analytics)

    def test_provider_falls_back_to_session_billing_provider(self):
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute("UPDATE session_model_usage SET billing_provider=''")
            conn.commit()
        finally:
            conn.close()
        snap = api.build_snapshot("7d", force=True, opener=FakeOpener({}), db_path=self.db)
        # Both rows belong to s1, whose session provider is provider-a.
        self.assertEqual([c["provider"] for c in snap["quota"]["cards"]], ["provider-a"])
        self.assertEqual([m["provider"] for m in snap["models"]], ["provider-a", "provider-a"])

    def test_lifetime_includes_all_history(self):
        opener = FakeOpener({})
        snap = api.build_snapshot("lifetime", force=True, opener=opener, db_path=self.db)
        self.assertEqual(snap["analytics"]["range"], "lifetime")
        self.assertEqual(snap["analytics"]["totalTokens"], 1225)
        self.assertEqual(snap["models"][0]["tokens"], 1165)

    def test_mixed_partial_sources(self):
        opener = FakeOpener({
            "/api/v1/health": {"app": "opencode-quota-gui", "version": "9"},
            "/api/v1/quota": make_quota_payload(),
        })
        snap = api.build_snapshot("7d", force=True, opener=opener, db_path=self.db)
        self.assertEqual(snap["source"], "mixed")
        self.assertTrue(snap["opencode"]["connected"])
        self.assertEqual(snap["quota"]["cards"][0]["id"], "card-abc")
        self.assertEqual(snap["analytics"]["totalTokens"], 225)
        self.assertEqual(snap["models"][0]["model"], "model-a")
        self.assertTrue(any(note.startswith("Usage/analytics") for note in snap["notes"]))

    def test_contract_shape(self):
        snap = api.build_snapshot("7d", force=True, opener=FakeOpener(full_responses()), db_path=self.db)
        self.assertIsInstance(snap["generatedAt"], str)
        self.assertIn(snap["source"], ("opencode", "hermes", "mixed"))
        self.assertIsInstance(snap["opencode"]["connected"], bool)
        self.assertIsInstance(snap["quota"], dict)
        self.assertIsInstance(snap["quota"]["cards"], list)
        for card in snap["quota"]["cards"]:
            for key in ("id", "provider", "label", "windows"):
                self.assertIn(key, card)
            self.assertIsInstance(card["windows"], list)
            for window in card["windows"]:
                for key in ("id", "label"):
                    self.assertIn(key, window)
        for key in ("range", "totalTokens", "cacheRead", "input", "reasoning", "output", "daily", "projects"):
            self.assertIn(key, snap["analytics"])
        self.assertIsInstance(snap["analytics"]["daily"], list)
        self.assertIsInstance(snap["analytics"]["projects"], list)
        self.assertIsInstance(snap["models"], list)
        self.assertIsInstance(snap["notes"], list)

    def test_endpoints_and_refresh_bypass_cache(self):
        opener = FakeOpener(full_responses("1"))
        with patch.object(api, "DEFAULT_OPENER", opener):
            health = self.client.get(PREFIX + "/health")
            self.assertEqual(health.status_code, 200)
            self.assertTrue(health.json()["ok"])
            first = self.client.get(PREFIX + "/snapshot?range=7d")
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json()["opencode"]["version"], "1")
            opener.responses = full_responses("2")
            cached = self.client.get(PREFIX + "/snapshot?range=7d")
            self.assertEqual(cached.json()["opencode"]["version"], "1")  # 5s cache
            refreshed = self.client.post(PREFIX + "/refresh", json={"range": "24h"})
            self.assertEqual(refreshed.status_code, 200)
            self.assertEqual(refreshed.json()["opencode"]["version"], "2")
            self.assertEqual(refreshed.json()["analytics"]["range"], "24h")
            after = self.client.get(PREFIX + "/snapshot?range=24h")
            self.assertEqual(after.json()["opencode"]["version"], "2")

    def test_endpoints_without_ogu_server(self):
        with patch.object(api, "DEFAULT_OPENER", FakeOpener({})):
            response = self.client.get(PREFIX + "/snapshot?range=7d")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["source"], "hermes")
            self.assertEqual(response.json()["analytics"]["totalTokens"], 225)
            self.assertNotIn("SECRET_TEST_TOKEN_DO_NOT_LEAK", response.text)
            bad_range = self.client.get(PREFIX + "/snapshot?range=bogus")
            self.assertEqual(bad_range.json()["analytics"]["range"], "7d")


class ProfileSecretScopeTests(unittest.TestCase):
    """Plugin routes run outside a turn scope: credentials need a profile scope."""

    def test_installs_profile_scope_when_none_is_active(self):
        fake = types.SimpleNamespace()
        fake.current_secret_scope = lambda: None
        fake.build_profile_secret_scope = lambda home: {"PROBE": "value"}
        installed, reset = [], []
        fake.set_secret_scope = lambda mapping: installed.append(mapping) or "TOKEN"
        fake.reset_secret_scope = reset.append
        with patch.dict(sys.modules, {"agent": types.SimpleNamespace(), "agent.secret_scope": fake}):
            result = api.call_with_profile_secrets(lambda: ("ok", 42))
        self.assertEqual(result, ("ok", 42))
        self.assertEqual(installed, [{"PROBE": "value"}])
        self.assertEqual(reset, ["TOKEN"])

    def test_existing_scope_is_not_replaced(self):
        fake = types.SimpleNamespace()
        fake.current_secret_scope = lambda: {"ALREADY": "scoped"}
        fake.set_secret_scope = lambda mapping: self.fail("scope must not be replaced")
        with patch.dict(sys.modules, {"agent": types.SimpleNamespace(), "agent.secret_scope": fake}):
            result = api.call_with_profile_secrets(lambda: "ok")
        self.assertEqual(result, "ok")

    def test_opencode_probe_is_opt_in(self):
        card = {"id": "probe", "provider": "probe", "label": "Probe", "active": True, "windows": []}
        api.invalidate_cache()
        with patch.object(api, "OQG_ENABLED", False), \
             patch.object(api, "fetch_json", side_effect=AssertionError("OQG no debe consultarse")), \
             patch.object(api, "call_with_profile_secrets", return_value=([card], [])):
            snap = api.build_snapshot("7d", force=True)
        self.assertFalse(snap["opencode"]["enabled"])
        self.assertFalse(snap["opencode"]["connected"])
        self.assertNotIn("error", snap["opencode"])
        self.assertEqual(snap["source"], "mixed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
