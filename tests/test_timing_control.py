import unittest
from datetime import date, timedelta
from decimal import Decimal
from random import Random

from market_sandbox.paper_replay import Limits, Portfolio
from market_sandbox.timing_control import (
    EXPOSURE_TOLERANCE_DIVISOR,
    Buy,
    ending_equity,
    exposure_days,
    matched_days,
)


class TimingControlTests(unittest.TestCase):
    def test_sample_keeps_months_unique_days_and_matches_exposure_by_symbol(self):
        sessions = [date(2024, 1, 1) + timedelta(days=index) for index in range(365)]
        buys = [Buy("SPY", 4, 25_000, 3), Buy("SPY", 8, 25_000, 3), Buy("QQQ", 36, 9_000, 1)]
        original = [buy.day for buy in buys]
        target = exposure_days(buys, original, len(sessions))
        sample = list(matched_days(buys, sessions, 20, Random(42)))
        self.assertEqual(len(sample), 20)
        self.assertEqual(sample, list(matched_days(buys, sessions, 20, Random(42))))
        self.assertTrue(any(days != original for days in sample))
        for days in sample:
            self.assertEqual(len(days), len(set(days)))
            for buy, day in zip(buys, days, strict=True):
                self.assertEqual(sessions[buy.day].month, sessions[day].month)
            sampled = exposure_days(buys, days, len(sessions))
            for symbol, expected in target.items():
                self.assertLessEqual(
                    abs(sampled[symbol] - expected) * EXPOSURE_TOLERANCE_DIVISOR,
                    expected,
                )

    def test_retiming_a_fill_changes_units_but_keeps_cash_spent(self):
        buys = [Buy("QQQ", 0, 405, 1)]
        opens = {"QQQ": [Decimal("100"), Decimal("80")]}
        closes = {"QQQ": Decimal("110")}
        original = Portfolio(Limits(500, 1_000))
        retimed = Portfolio(Limits(500, 1_000))
        observed_equity = ending_equity(buys, [0], opens, closes, original)
        control_equity = ending_equity(buys, [1], opens, closes, retimed)
        self.assertEqual(original.cash_cents, retimed.cash_cents)
        self.assertEqual(original.cash_cents, 1_000_000 - 405)
        self.assertGreater(control_equity, observed_equity)
