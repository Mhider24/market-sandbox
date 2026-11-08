import unittest
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from market_sandbox.demo import demo_market
from market_sandbox.forecast import MIN_CLOSES, Forecast, forecast_next_close, walk_forward_forecasts
from market_sandbox.paper_replay import Bar, Limits, Portfolio, replay


class PaperReplayTests(unittest.TestCase):
    def test_replay_matches_live_forecast_and_trailing_gate(self):
        _, assets = demo_market()
        closes = assets[0].closes[: MIN_CLOSES + 3]
        forecasts = walk_forward_forecasts(closes, MIN_CLOSES)
        expected = [forecast_next_close(closes[:target]) for target in range(MIN_CLOSES, len(closes))]
        self.assertEqual(forecasts, expected)

    def test_fill_uses_open_with_slippage_and_charges_fee_within_budget(self):
        portfolio = Portfolio(Limits(2_000, 4_000))
        spent, fee = portfolio.buy("SPY", Decimal("100"), 25_000)
        self.assertEqual((spent, fee), (25_000, 3))
        self.assertEqual(portfolio.cash_cents, 975_000)
        self.assertAlmostEqual(portfolio.units["SPY"], Decimal("249.97") / Decimal("100.05"))
        portfolio.mark_close({"SPY": Decimal("110")})
        self.assertEqual(portfolio.equity_cents, 1_002_482)

    def test_gap_up_rechecks_open_exposure_before_filling(self):
        first_date = date(2024, 1, 1)
        dates = [first_date + timedelta(days=day) for day in range(MIN_CLOSES + 2)]
        quiet = [Bar(session, Decimal("100"), Decimal("100")) for session in dates]
        gapped = quiet[:-1] + [Bar(dates[-1], Decimal("400"), Decimal("400"))]
        eligible = Forecast(30, 10, 80, 100, 40)
        blocked = Forecast(30, 10, 110, 100, 40)
        with patch(
            "market_sandbox.paper_replay.walk_forward_forecasts",
            side_effect=[[eligible, eligible], [blocked, blocked]],
        ):
            result = replay({"SPY": gapped, "QQQ": quiet}, dates[-2], dates[-1])

        self.assertEqual(result.gate_scores["SPY"].count, 2)
        self.assertEqual(result.gate_scores["QQQ"].count, 0)
        self.assertEqual(result.portfolios["standard"].fills, 2)
        self.assertEqual(result.portfolios["tight"].fills, 1)
        self.assertEqual(result.fill_scores["tight"].count, 1)
        last_tight_spy = [
            row for row in result.log if row.profile == "tight" and row.symbol == "SPY"
        ][-1]
        self.assertEqual(last_tight_spy.decision_close, dates[-2])
        self.assertEqual(last_tight_spy.target_session, dates[-1])
        self.assertEqual(last_tight_spy.action, "unfilled")
        self.assertGreater(last_tight_spy.proposed_cents, 0)
        self.assertEqual(last_tight_spy.spent_cents, 0)
        self.assertIn("exposure has reached its limit", last_tight_spy.reason)
