"""Compare paper fills against randomized entry days with similar exposure."""

import argparse
import csv
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from random import Random

import numpy as np

from .paper_replay import Bar, Portfolio, ReplayResult, load_bars, replay

CONTROL_RUNS = 2_000
SEED = 42
EXPOSURE_TOLERANCE_DIVISOR = 200  # 0.5% per ETF


@dataclass(frozen=True)
class Buy:
    symbol: str
    day: int
    spent_cents: int
    fee_cents: int


@dataclass(frozen=True)
class TimingReport:
    fills: int
    observed_cents: int
    control_p05_cents: float
    control_median_cents: float
    control_p95_cents: float
    share_below_observed: float


def exposure_days(buys: list[Buy], days: list[int], session_count: int) -> Counter[str]:
    """Count invested notional times remaining trading sessions, by ETF."""
    exposure = Counter()
    for buy, day in zip(buys, days, strict=True):
        exposure[buy.symbol] += (buy.spent_cents - buy.fee_cents) * (session_count - day)
    return exposure


def matched_days(
    buys: list[Buy], sessions: list[date], runs: int, rng: Random
) -> Iterator[list[int]]:
    days_by_month = defaultdict(list)
    for index, session in enumerate(sessions):
        days_by_month[session.year, session.month].append(index)

    buys_by_month = defaultdict(list)
    for index, buy in enumerate(buys):
        session = sessions[buy.day]
        buys_by_month[session.year, session.month].append(index)

    original = exposure_days(buys, [buy.day for buy in buys], len(sessions))
    accepted = 0
    for _ in range(max(10_000, runs * 100)):
        days = [0] * len(buys)
        for month, indexes in buys_by_month.items():
            for index, day in zip(indexes, rng.sample(days_by_month[month], len(indexes)), strict=True):
                days[index] = day

        sampled = exposure_days(buys, days, len(sessions))
        if all(
            abs(sampled[symbol] - target) * EXPOSURE_TOLERANCE_DIVISOR <= target
            for symbol, target in original.items()
        ):
            yield days
            accepted += 1
            if accepted == runs:
                return

    raise ValueError(
        f"Found only {accepted} of {runs} requested exposure matches. "
        "The trade schedule may leave too few eligible alternatives."
    )


def ending_equity(
    buys: list[Buy],
    days: list[int],
    opens: dict[str, list[Decimal]],
    closes: dict[str, Decimal],
    portfolio: Portfolio,
) -> int:
    for buy, day in zip(buys, days, strict=True):
        # Preserve the recorded dollar amount and fee when moving the entry.
        portfolio.buy(buy.symbol, opens[buy.symbol][day], buy.spent_cents)
    portfolio.mark_close(closes)
    return portfolio.equity_cents


def compare_timing(
    market: dict[str, list[Bar]], result: ReplayResult, runs: int = CONTROL_RUNS, seed: int = SEED
) -> dict[str, TimingReport]:
    if runs < 1:
        raise ValueError("Control runs must be positive.")

    sessions = list(dict.fromkeys(row.target_session for row in result.log))
    indexes = {day: index for index, day in enumerate(sessions)}
    bars_by_symbol = {
        symbol: {bar.session: bar for bar in history} for symbol, history in market.items()
    }
    opens = {
        symbol: [bars[session].adjusted_open for session in sessions]
        for symbol, bars in bars_by_symbol.items()
    }
    closes = {symbol: bars[sessions[-1]].adjusted_close for symbol, bars in bars_by_symbol.items()}

    reports = {}
    for name, observed in result.portfolios.items():
        buys = [
            Buy(row.symbol, indexes[row.target_session], row.spent_cents, row.fee_cents)
            for row in result.log
            if row.profile == name and row.action == "filled"
        ]
        if not buys:
            continue

        controls = [
            ending_equity(buys, days, opens, closes, Portfolio(observed.limits))
            for days in matched_days(buys, sessions, runs, Random(seed))
        ]
        p05, median, p95 = np.quantile(controls, [0.05, 0.50, 0.95])
        reports[name] = TimingReport(
            fills=len(buys),
            observed_cents=observed.equity_cents,
            control_p05_cents=float(p05),
            control_median_cents=float(median),
            control_p95_cents=float(p95),
            share_below_observed=sum(value < observed.equity_cents for value in controls) / runs,
        )
    return reports


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spy-csv", required=True, type=Path)
    parser.add_argument("--qqq-csv", required=True, type=Path)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    try:
        market = {"SPY": load_bars(args.spy_csv), "QQQ": load_bars(args.qqq_csv)}
        result = replay(market, args.start, args.end)
        reports = compare_timing(market, result)
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        parser.error(str(exc))

    first, last = result.log[0].target_session, result.log[-1].target_session
    print(f"Target sessions: {first} to {last} | {CONTROL_RUNS:,} controls per profile | seed {SEED}")
    for name in result.portfolios:
        if name not in reports:
            print(f"{name}: 0 fills to compare")
            continue

        report = reports[name]
        print(
            f"{name}: {report.fills} fills | observed ${report.observed_cents / 100:,.2f} "
            f"| control median ${report.control_median_cents / 100:,.2f} "
            f"| 5th–95th percentile ${report.control_p05_cents / 100:,.2f}"
            f"–${report.control_p95_cents / 100:,.2f} "
            f"| controls below observed {report.share_below_observed:.1%}"
        )


if __name__ == "__main__":
    main()
