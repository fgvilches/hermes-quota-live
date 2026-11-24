"""Tests for direct provider quota adapters (all HTTP is faked; no secrets)."""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).resolve().parents[1]
ADAPTER_FILE = PLUGIN_DIR / "dashboard" / "quota_adapters.py"
SPEC = importlib.util.spec_from_file_location("hermes_quota_adapters", str(ADAPTER_FILE))
qa = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = qa
SPEC.loader.exec_module(qa)

SECRET = "SUPERSECRET_VALUE_SHOULD_NEVER_APPEAR"


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def opener_for(responses):
    def opener(request, timeout=None):
        url = request.full_url
        for fragment, payload in responses.items():
            if fragment in url:
                return FakeResponse(payload)
        raise AssertionError("unexpected URL: " + url)

    return opener


class DirectAdapterTests(unittest.TestCase):
    def test_opencode_go_maps_usd_windows(self):
        responses = {
            "zen/go/v1/usage": {
                "usage": {
                    "rolling": {"status": "ok", "percent": 0, "resetsAt": "2026-11-23T00:08:24Z"},
                    "weekly": {"status": "rate-limited", "percent": 100, "resetsAt": "2026-11-23T00:00:00Z"},
                    "monthly": {"status": "ok", "percent": 81, "resetsAt": "2026-12-02T19:55:59Z"},
                }
            },
            "console/api/go/status": {
                "access": {
                    "meters": {
                        "fiveHour": {"usedMicroCents": "0", "limitMicroCents": "1200000000"},
                        "week": {"usedMicroCents": "3000000000", "limitMicroCents": "3000000000"},
                        "month": {"usedMicroCents": "4889000000", "limitMicroCents": "6000000000"},
                    }
                }
            },
        }
        with patch.object(qa, "_workspace_id", lambda: "wrk_test"):
            card = qa.fetch_opencode_go(SECRET, opener=opener_for(responses))
        self.assertEqual(card["provider"], "opencode-go")
        self.assertEqual(len(card["windows"]), 3)
        rolling, weekly, monthly = card["windows"]
        self.assertEqual((rolling["used"], rolling["limit"]), (0.0, 12.0))
        self.assertEqual(rolling["remainingPct"], 100.0)
        self.assertEqual(weekly["remainingPct"], 0.0)
        self.assertEqual(weekly["status"], "rate-limited")
        self.assertEqual(monthly["remainingPct"], 18.5)
        self.assertTrue(all(w["unit"] == "usd" for w in card["windows"]))
        self.assertNotIn(SECRET, json.dumps(card))

    def test_opencode_go_falls_back_to_percent_without_workspace(self):
        responses = {
            "zen/go/v1/usage": {
                "usage": {"rolling": {"percent": 40, "status": "ok"}}
            }
        }
        with patch.object(qa, "_workspace_id", lambda: None):
            card = qa.fetch_opencode_go(SECRET, opener=opener_for(responses))
        window = card["windows"][0]
        self.assertEqual((window["used"], window["limit"], window["remainingPct"]), (40, 100, 60))
        self.assertEqual(window["unit"], "percent")

    def test_openrouter_uses_budget_and_free_requests(self):
        responses = {
            "api/v1/key": {
                "data": {
                    "budgets": [
                        {
                            "id": "b1",
                            "name": "Mensual",
                            "limit_usd": 10,
                            "spend_usd": 2.5,
                            "remaining_usd": 7.5,
                            "resets_at": "2026-12-01T00:00:00Z",
                            "scope": "monthly",
                        }
                    ],
                    "free_model_daily_requests": {"used": 7, "limit": 50},
                }
            }
        }
        card = qa.fetch_openrouter(SECRET, opener=opener_for(responses))
        self.assertEqual(len(card["windows"]), 2)
        budget, free = card["windows"]
        self.assertEqual((budget["used"], budget["limit"], budget["remainingPct"]), (2.5, 10, 75.0))
        self.assertEqual((free["used"], free["limit"], free["unit"]), (7, 50, "requests"))
        self.assertNotIn(SECRET, json.dumps(card))

    def test_openrouter_does_not_invent_a_credit_limit(self):
        responses = {
            "api/v1/key": {
                "data": {"limit": None, "budgets": [], "free_model_daily_requests": {"used": 0, "limit": 50}}
            }
        }
        card = qa.fetch_openrouter(SECRET, opener=opener_for(responses))
        self.assertEqual([w["id"] for w in card["windows"]], ["free_daily_requests"])

    def test_copilot_maps_only_categories_with_quota(self):
        responses = {
            "copilot_internal/user": {
                "copilot_plan": "individual",
                "quota_reset_date_utc": "2026-12-01T00:00:00Z",
                "quota_snapshots": {
                    "chat": {
                        "has_quota": True,
                        "entitlement": 200,
                        "remaining": 150,
                        "percent_remaining": 75,
                        "token_based_billing": True,
                    },
                    "premium_interactions": {"has_quota": False, "entitlement": 0},
                },
            }
        }
        card = qa.fetch_copilot(SECRET, opener=opener_for(responses))
        self.assertEqual(len(card["windows"]), 1)
        window = card["windows"][0]
        self.assertEqual((window["used"], window["limit"], window["unit"]), (50, 200, "credits"))
        self.assertEqual(window["status"], "individual")

    def test_zai_without_plan_is_reported_not_fabricated(self):
        card = qa.fetch_zai(SECRET, opener=opener_for({"quota/limit": {"success": False, "code": 500}}))
        self.assertEqual(card["windows"], [])
        self.assertIn("no coding plan", card["error"])
        self.assertFalse(card["active"])

    def test_collect_direct_cards_is_sanitized(self):
        responses = {
            "zen/go/v1/usage": {"usage": {"rolling": {"percent": 5}}},
            "api/v1/key": {"data": {"free_model_daily_requests": {"used": 1, "limit": 50}}},
            "copilot_internal/user": {
                "quota_snapshots": {
                    "chat": {
                        "has_quota": True,
                        "entitlement": 10,
                        "remaining": 8,
                        "percent_remaining": 80,
                    }
                }
            },
            "quota/limit": {"success": False, "code": 500},
        }
        opener = opener_for(responses)
        with patch.object(qa, "credential", lambda provider: SECRET), patch.object(
            qa, "_workspace_id", lambda: None
        ):
            cards, notes = qa.collect_direct_cards(opener=opener)
        self.assertEqual(
            [card["provider"] for card in cards],
            ["opencode-go", "openrouter", "copilot", "zai"],
        )
        rendered = json.dumps({"cards": cards, "notes": notes})
        self.assertNotIn(SECRET, rendered)
        self.assertTrue(any("Direct provider quota" in note for note in notes))


class CredentialSourceTests(unittest.TestCase):
    def test_copilot_uses_declared_secret_first(self):
        with patch.object(qa, "_secret", side_effect=lambda n: SECRET if n == "GH_TOKEN" else None):
            self.assertEqual(qa.credential("copilot"), SECRET)

    def test_copilot_uses_github_token_second(self):
        with patch.object(qa, "_secret", side_effect=lambda n: SECRET if n == "GITHUB_TOKEN" else None):
            self.assertEqual(qa.credential("copilot"), SECRET)

    def test_no_local_cli_fallback(self):
        with patch.object(qa, "_secret", return_value=None):
            self.assertIsNone(qa.credential("copilot"))
            self.assertIsNone(qa.credential("zai"))


if __name__ == "__main__":
    unittest.main()
