import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np

from market_sandbox.backtest import load_prices, run_benchmark
from market_sandbox.demo import demo_market
from market_sandbox.forecast import (
    BPS_PER_UNIT,
    MIN_TRAINING_CLOSES,
    forecast_next_close,
    walk_forward_returns,
)


class BacktestTests(unittest.TestCase):
    def setUp(self):
        self.sessions, assets = demo_market()
        self.closes = assets[0].closes

    def test_replay_matches_forecasts_made_before_each_target(self):
        start = 160
        closes = self.closes[:163]
        actual, predicted = walk_forward_returns(closes, start)
        for target in range(start, len(closes)):
            offset = target - start
            self.assertAlmostEqual(actual[offset], np.log(closes[target] / closes[target - 1]))
            expected = forecast_next_close(closes[:target]).next_log_return_bps
            self.assertAlmostEqual(predicted[offset] * BPS_PER_UNIT, expected)

    def test_holdout_uses_both_date_boundaries(self):
        prices = list(zip(self.sessions, self.closes))
        report = run_benchmark(prices, self.sessions[160], self.sessions[169])
        self.assertEqual(report.first_target, self.sessions[160])
        self.assertEqual(report.last_target, self.sessions[169])
        self.assertEqual(report.sessions, 10)
        actual = np.diff(np.log(self.closes[159:170]))
        self.assertAlmostEqual(report.baseline_rmse_bps, np.sqrt(np.mean(actual**2)) * BPS_PER_UNIT)

    def test_rejects_a_holdout_with_insufficient_warmup(self):
        prices = list(zip(self.sessions, self.closes))
        with self.assertRaisesRegex(ValueError, "81 trading closes"):
            run_benchmark(prices, self.sessions[MIN_TRAINING_CLOSES - 1], self.sessions[-1])

    def test_accepts_latest_first_csv_and_an_adjusted_close_column(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prices.csv"
            path.write_text(
                "Date,Close,Adj Close\n2025-01-03,102,99\n2025-01-02,101,98\n",
                encoding="utf-8-sig",
            )
            self.assertEqual(
                load_prices(path, "Adj Close"),
                [(date(2025, 1, 2), 98.0), (date(2025, 1, 3), 99.0)],
            )

    def test_rejects_duplicate_sessions_and_nonfinite_prices(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prices.csv"
            path.write_text("Date,Close\n2025-01-02,100\n2025-01-02,101\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Duplicate trading date"):
                load_prices(path, "Close")
            path.write_text("Date,Close\n2025-01-02,nan\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "finite and positive"):
                load_prices(path, "Close")
