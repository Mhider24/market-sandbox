"""Replay close-time buy proposals with next-session paper fills."""

import argparse
import csv
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from math import log, sqrt
from pathlib import Path

from .backtest import load_prices
from .forecast import BPS_PER_UNIT, MIN_CLOSES, walk_forward_forecasts
from .policy import Candidate, Decision, check_candidate, forecast_hold_reason, select_candidate

STARTING_CASH_CENTS = 1_000_000
ORDER_BUDGET_CENTS = 25_000
SLIPPAGE_BPS = 5
FEE_BPS = 1
EXPOSURE_GROUP = "equity-etf"


@dataclass(frozen=True)
class Bar:
    session: date
    adjusted_open: Decimal
    adjusted_close: Decimal


@dataclass(frozen=True)
class Limits:
    stock_bps: int
    group_bps: int


PROFILES = {"standard": Limits(2_000, 4_000), "tight": Limits(500, 1_000)}


@dataclass
class Portfolio:
    limits: Limits
    cash_cents: int = STARTING_CASH_CENTS
    units: dict[str, Decimal] = field(default_factory=dict)
    fills: int = 0
    peak_cents: int = STARTING_CASH_CENTS
    equity_cents: int = STARTING_CASH_CENTS
    max_drawdown: float = 0.0

    def assess(
        self, candidate: Candidate, prices: dict[str, Decimal], cost_reserve: int = 0
    ) -> Decision:
        values = self.values(prices)
        invested = sum(values.values())
        return check_candidate(
            candidate,
            cash_cents=self.cash_cents,
            portfolio_cents=max(0, self.cash_cents + invested - cost_reserve),
            position_cents=values.get(candidate.symbol, 0),
            sector_cents=invested,
            order_budget_cents=ORDER_BUDGET_CENTS,
            max_stock_bps=self.limits.stock_bps,
            max_sector_bps=self.limits.group_bps,
            excluded_sectors=set(),
        )

    def values(self, prices: dict[str, Decimal]) -> dict[str, int]:
        return {symbol: int(units * prices[symbol] * 100) for symbol, units in self.units.items()}

    def buy(self, symbol: str, opening_price: Decimal, budget_cents: int) -> tuple[int, int]:
        # The proposal budget includes fees. Round fees up to the next cent.
        notional = budget_cents * BPS_PER_UNIT // (BPS_PER_UNIT + FEE_BPS)
        if notional == 0:
            return 0, 0

        fee = (notional * FEE_BPS + BPS_PER_UNIT - 1) // BPS_PER_UNIT
        fill_price = opening_price * (BPS_PER_UNIT + SLIPPAGE_BPS) / BPS_PER_UNIT
        self.units[symbol] = self.units.get(symbol, Decimal(0)) + notional / (100 * fill_price)
        self.cash_cents -= notional + fee
        self.fills += 1
        return notional + fee, fee

    def mark_close(self, closes: dict[str, Decimal]) -> None:
        self.equity_cents = self.cash_cents + sum(self.values(closes).values())
        self.peak_cents = max(self.peak_cents, self.equity_cents)
        self.max_drawdown = max(self.max_drawdown, 1 - self.equity_cents / self.peak_cents)


@dataclass
class ForecastScore:
    count: int = 0
    ridge_squared_error: float = 0.0
    baseline_squared_error: float = 0.0

    def add(self, actual: float, prediction: float) -> None:
        self.count += 1
        self.ridge_squared_error += (actual - prediction) ** 2
        self.baseline_squared_error += actual**2

    def describe(self) -> str:
        if self.count == 0:
            return "0 sessions"
        ridge = sqrt(self.ridge_squared_error / self.count)
        baseline = sqrt(self.baseline_squared_error / self.count)
        return f"{self.count} sessions | Ridge {ridge:.3f} bps | zero return {baseline:.3f} bps"


@dataclass(frozen=True)
class LogRow:
    profile: str
    decision_close: date
    target_session: date
    symbol: str
    forecast_bps: float
    actual_bps: float
    forecast_gate: str
    action: str
    reason: str
    proposed_cents: int
    spent_cents: int
    fee_cents: int
    closing_equity_cents: int


@dataclass
class ReplayResult:
    gate_scores: dict[str, ForecastScore]
    portfolios: dict[str, Portfolio]
    fill_scores: dict[str, ForecastScore]
    log: list[LogRow]


def load_bars(path: Path) -> list[Bar]:
    opens = load_prices(path, "Open")
    closes = load_prices(path, "Close")
    adjusted = load_prices(path, "Adj Close")
    bars = []
    for (session, opening), (_, close), (_, adjusted_close) in zip(
        opens, closes, adjusted, strict=True
    ):
        adjusted_price = Decimal(str(adjusted_close))
        adjusted_open = Decimal(str(opening)) * adjusted_price / Decimal(str(close))
        bars.append(Bar(session, adjusted_open, adjusted_price))
    return bars


def replay(
    market: dict[str, list[Bar]], start_date: date, end_date: date
) -> ReplayResult:
    if end_date < start_date:
        raise ValueError("End date must be on or after the start date.")
    if not market:
        raise ValueError("Supply at least one price history.")
    dates = [bar.session for bar in next(iter(market.values()))]
    if any([bar.session for bar in bars] != dates for bars in market.values()):
        raise ValueError("Price CSVs must contain the same trading dates.")

    start = bisect_left(dates, start_date)
    end = bisect_right(dates, end_date)
    if start >= end:
        raise ValueError("The CSVs have no common sessions in the requested period.")
    if start < MIN_CLOSES:
        raise ValueError(f"Need at least {MIN_CLOSES} closes before the first replay session.")

    forecasts = {
        symbol: walk_forward_forecasts([float(bar.adjusted_close) for bar in bars[:end]], start)
        for symbol, bars in market.items()
    }
    portfolios = {name: Portfolio(limits) for name, limits in PROFILES.items()}
    gate_scores = {symbol: ForecastScore() for symbol in market}
    fill_scores = {name: ForecastScore() for name in portfolios}
    log_rows = []
    for index in range(start, end):
        previous = {symbol: bars[index - 1].adjusted_close for symbol, bars in market.items()}
        opening = {symbol: bars[index].adjusted_open for symbol, bars in market.items()}
        closing = {symbol: bars[index].adjusted_close for symbol, bars in market.items()}
        candidates = [
            Candidate(symbol, EXPOSURE_GROUP, series[index - start])
            for symbol, series in forecasts.items()
        ]
        actual = {
            symbol: log(float(closing[symbol] / previous[symbol])) * BPS_PER_UNIT
            for symbol in market
        }
        gate_reasons = {
            candidate.symbol: forecast_hold_reason(candidate.forecast) for candidate in candidates
        }
        for candidate in candidates:
            if gate_reasons[candidate.symbol] is None:
                gate_scores[candidate.symbol].add(
                    actual[candidate.symbol], candidate.forecast.next_log_return_bps
                )

        for name, portfolio in portfolios.items():
            assessments = [(candidate, portfolio.assess(candidate, previous)) for candidate in candidates]
            selected = select_candidate(assessments, set())
            spent = fee = 0
            fill_reason = ""
            if selected is not None:
                candidate, proposal = selected
                # Reserve costs and cent rounding when rechecking caps at the open.
                reserve = (
                    proposal.max_buy_cents * (SLIPPAGE_BPS + FEE_BPS) + BPS_PER_UNIT - 1
                ) // BPS_PER_UNIT + 2
                at_open = portfolio.assess(candidate, opening, reserve)
                budget = min(proposal.max_buy_cents, at_open.max_buy_cents)
                spent, fee = portfolio.buy(candidate.symbol, opening[candidate.symbol], budget)
                if spent:
                    fill_reason = "Filled at next open with modeled costs."
                    fill_scores[name].add(
                        actual[candidate.symbol], candidate.forecast.next_log_return_bps
                    )
                else:
                    fill_reason = at_open.reason if at_open.action == "hold" else "Order too small after fees."

            portfolio.mark_close(closing)
            for candidate, assessment in assessments:
                symbol = candidate.symbol
                is_selected = selected is not None and selected[0].symbol == symbol
                action, reason = "hold", assessment.reason
                if is_selected:
                    action = "filled" if spent else "unfilled"
                    reason = fill_reason
                elif assessment.action == "eligible":
                    reason = "Another candidate ranked higher."

                log_rows.append(
                    LogRow(
                        name, dates[index - 1], dates[index], symbol,
                        candidate.forecast.next_log_return_bps, actual[symbol],
                        gate_reasons[symbol] or "passed", action, reason,
                        assessment.max_buy_cents if is_selected else 0,
                        spent if is_selected else 0,
                        fee if is_selected else 0,
                        portfolio.equity_cents,
                    )
                )

    return ReplayResult(gate_scores, portfolios, fill_scores, log_rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spy-csv", required=True, type=Path)
    parser.add_argument("--qqq-csv", required=True, type=Path)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--log", type=Path, help="Optional path for the daily decision CSV.")
    args = parser.parse_args()
    try:
        market = {"SPY": load_bars(args.spy_csv), "QQQ": load_bars(args.qqq_csv)}
        result = replay(market, args.start, args.end)
        rows = result.log
        if args.log:
            if args.log.resolve() in {args.spy_csv.resolve(), args.qqq_csv.resolve()}:
                raise ValueError("Decision log must have a different path from the price CSVs.")
            with args.log.open("x", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(asdict(rows[0])))
                writer.writeheader()
                writer.writerows(asdict(row) for row in rows)
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        parser.error(str(exc))

    print(f"Target sessions: {rows[0].target_session} to {rows[-1].target_session}")
    for symbol, score in result.gate_scores.items():
        print(f"{symbol} forecast gate: {score.describe()}")
    for name, portfolio in result.portfolios.items():
        change = portfolio.equity_cents / STARTING_CASH_CENTS - 1
        print(
            f"{name}: {portfolio.fills} fills | final equity ${portfolio.equity_cents / 100:,.2f} "
            f"({change:+.2%}) | max drawdown {portfolio.max_drawdown:.2%}"
        )
        print(f"  Forecasts on filled orders: {result.fill_scores[name].describe()}")
    if args.log:
        print(f"Decision log: {args.log}")


if __name__ == "__main__":
    main()
