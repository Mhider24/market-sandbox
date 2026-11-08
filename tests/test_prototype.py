import unittest

import numpy as np
from pydantic import ValidationError

from market_sandbox.api import Simulation, simulate
from market_sandbox.demo import demo_market, demo_requests, run_demo
from market_sandbox.forecast import (
    MIN_CLOSES,
    _features,
    _predict,
    forecast_next_close,
)


class ForecastTests(unittest.TestCase):
    def test_future_prices_do_not_change_past_feature_rows(self):
        history = np.linspace(-0.01, 0.01, 100)
        expanded = np.append(history, [0.07, -0.08])
        np.testing.assert_array_equal(_features(history), _features(expanded)[:-2])

    def test_forecast_does_not_use_current_or_future_targets(self):
        returns = np.linspace(-0.01, 0.01, 100)
        features = _features(returns)
        targets = returns[20:].copy()
        index = 65
        expected = _predict(features, targets, index)
        targets[index:] = 1
        self.assertEqual(_predict(features, targets, index), expected)

    def test_appending_a_close_changes_latest_forecast(self):
        _, assets = demo_market()
        closes = assets[0].closes
        earlier = forecast_next_close(closes[:-1])
        latest = forecast_next_close(closes)
        self.assertNotAlmostEqual(earlier.next_log_return_bps, latest.next_log_return_bps)

    def test_minimum_history_still_yields_forty_validation_sessions(self):
        forecast = forecast_next_close([100.0] * MIN_CLOSES)
        self.assertEqual(forecast.validation_sessions, 40)
        self.assertEqual(forecast.baseline_rmse_bps, 0)
        self.assertFalse(forecast.beats_baseline)


class SimulationTests(unittest.TestCase):
    def test_same_market_yields_different_results_by_exposure(self):
        result = run_demo()
        self.assertEqual(result["room_for_tech"]["proposal"]["action"], "paper_buy")
        self.assertEqual(result["room_for_tech"]["proposal"]["amount_cents"], 5_000)
        at_limit = result["tech_at_limit"]
        self.assertEqual(at_limit["proposal"]["action"], "hold")
        self.assertIn("Sector exposure", at_limit["candidates"][0]["reason"])

    def test_paper_buy_respects_remaining_position_room(self):
        request = demo_requests()["room_for_tech"]
        request.profile.cash_cents = 81_000
        request.profile.holdings_cents = {"DEMO-TECH": 19_000}
        self.assertEqual(simulate(request)["proposal"]["amount_cents"], 1_000)

    def test_holds_when_model_cannot_beat_baseline(self):
        request = demo_requests()["room_for_tech"]
        request.assets[0].closes = [100.0] * len(request.sessions)
        result = simulate(request)
        self.assertEqual(result["proposal"]["action"], "hold")
        self.assertIn("zero-return baseline", result["candidates"][0]["reason"])

    def test_rejects_a_missing_price_or_unknown_holding(self):
        payload = demo_requests()["room_for_tech"].model_dump()
        payload["assets"][0]["closes"].pop()
        with self.assertRaisesRegex(ValidationError, "one close per session"):
            Simulation.model_validate(payload)
        payload = demo_requests()["room_for_tech"].model_dump()
        payload["profile"]["holdings_cents"] = {"UNKNOWN": 1}
        with self.assertRaisesRegex(ValidationError, "matching asset"):
            Simulation.model_validate(payload)
