import unittest

from pydantic import ValidationError

from market_sandbox.api import RiskRequest, risk_report


class RiskReportTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "as_of_session": "2026-11-06",
            "cash_cents": 630_000,
            "assets": [
                {"symbol": " qqq ", "sector": "equity-etf", "shares": "6", "close_cents": 45_000},
                {"symbol": "SPY", "sector": "EQUITY-ETF", "shares": "2", "close_cents": 50_000},
            ],
        }

    def report(self, symbol, amount):
        self.payload["paper_buy"] = {"symbol": symbol, "amount_cents": amount}
        return risk_report(RiskRequest.model_validate(self.payload))

    def test_marks_exposure_and_projects_both_affected_caps(self):
        result = self.report("QQQ", 50_000)
        current = result["current"]
        qqq = current["positions"][0]
        self.assertEqual((qqq["symbol"], qqq["weight_bps"], qqq["excess_bps"]), ("QQQ", 2700, 700))
        self.assertTrue(qqq["over_limit"])
        self.assertEqual(current["sectors"][0]["weight_bps"], 3700)
        self.assertEqual(current["sectors"][0]["room_cents"], 30_000)

        preview = result["preview"]
        self.assertEqual(preview["max_buy_cents"], 0)
        self.assertEqual(preview["status"], "exceeds_limits")
        projected = preview["projected"]
        self.assertEqual(projected["total_cents"], current["total_cents"])
        self.assertEqual(projected["cash_cents"], 580_000)
        self.assertEqual(projected["positions"][0]["weight_bps"], 3200)
        self.assertEqual(projected["sectors"][0]["weight_bps"], 4200)
        self.assertTrue(projected["sectors"][0]["over_limit"])

    def test_accepts_a_buy_at_the_exact_sector_cap(self):
        result = self.report("SPY", 30_000)
        preview = result["preview"]
        self.assertEqual(preview["max_buy_cents"], 30_000)
        self.assertEqual(preview["status"], "within_limits")
        sector = preview["projected"]["sectors"][0]
        self.assertEqual(sector["weight_bps"], 4000)
        self.assertEqual(sector["room_cents"], 0)
        self.assertFalse(sector["over_limit"])

    def test_insufficient_cash_has_no_projected_portfolio(self):
        preview = self.report("SPY", 630_001)["preview"]
        self.assertEqual(preview["status"], "insufficient_cash")
        self.assertIsNone(preview["projected"])

    def test_marks_fractional_shares_and_handles_empty_portfolios(self):
        payload = {
            "as_of_session": "2026-11-06",
            "cash_cents": 0,
            "assets": [
                {"symbol": "SPY", "sector": "equity-etf", "shares": "0.125", "close_cents": 100},
            ],
        }
        result = risk_report(RiskRequest.model_validate(payload))
        self.assertEqual(result["current"]["positions"][0]["market_value_cents"], 13)
        self.assertIsNone(result["preview"])

        payload["assets"] = []
        current = risk_report(RiskRequest.model_validate(payload))["current"]
        self.assertEqual(current["total_cents"], 0)
        self.assertIsNone(current["cash_weight_bps"])
        self.assertEqual(current["positions"], [])

    def test_rejects_unknown_buys_and_normalized_duplicate_symbols(self):
        with self.assertRaisesRegex(ValidationError, "matching asset"):
            self.report("VTI", 100)
        self.payload.pop("paper_buy")
        self.payload["assets"][1]["symbol"] = "QQQ"
        with self.assertRaisesRegex(ValidationError, "symbols must be unique"):
            RiskRequest.model_validate(self.payload)
