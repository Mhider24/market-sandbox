from dataclasses import dataclass
from typing import Literal

from .forecast import BPS_PER_UNIT, Forecast

MIN_FORECAST_BPS = 10.0
DEFAULT_STOCK_CAP_BPS = 2_000
DEFAULT_SECTOR_CAP_BPS = 4_000


@dataclass(frozen=True)
class Candidate:
    symbol: str
    sector: str
    forecast: Forecast


@dataclass(frozen=True)
class Decision:
    symbol: str
    action: Literal["eligible", "hold"]
    reason: str
    max_buy_cents: int = 0


def forecast_hold_reason(forecast: Forecast) -> str | None:
    if not forecast.beats_baseline:
        return "Ridge did not beat the zero-return baseline."
    if forecast.next_log_return_bps < MIN_FORECAST_BPS:
        return "Forecast is below the 10 basis point threshold."
    return None


def select_candidate(
    assessments: list[tuple[Candidate, Decision]], preferred_sectors: set[str]
) -> tuple[Candidate, Decision] | None:
    return min(
        ((candidate, decision) for candidate, decision in assessments if decision.action == "eligible"),
        key=lambda entry: (
            entry[0].sector not in preferred_sectors,
            -entry[0].forecast.next_log_return_bps,
            entry[0].symbol,
        ),
        default=None,
    )


def concentration_room_cents(total_cents: int, exposure_cents: int, limit_bps: int) -> int:
    return total_cents * limit_bps // BPS_PER_UNIT - exposure_cents


def check_candidate(
    candidate: Candidate,
    *,
    cash_cents: int,
    portfolio_cents: int,
    position_cents: int,
    sector_cents: int,
    order_budget_cents: int,
    max_stock_bps: int,
    max_sector_bps: int,
    excluded_sectors: set[str],
) -> Decision:
    symbol = candidate.symbol
    if candidate.sector in excluded_sectors:
        return Decision(symbol, "hold", "Sector excluded by the profile.")

    sector_room = concentration_room_cents(portfolio_cents, sector_cents, max_sector_bps)
    if sector_room <= 0:
        return Decision(symbol, "hold", "Sector exposure has reached its limit.")

    stock_room = concentration_room_cents(portfolio_cents, position_cents, max_stock_bps)
    if stock_room <= 0:
        return Decision(symbol, "hold", "Stock exposure has reached its limit.")

    max_buy = min(cash_cents, order_budget_cents, stock_room, sector_room)
    if max_buy <= 0:
        return Decision(symbol, "hold", "No virtual cash or order budget is available.")

    hold_reason = forecast_hold_reason(candidate.forecast)
    if hold_reason is not None:
        return Decision(symbol, "hold", hold_reason)

    return Decision(symbol, "eligible", "Forecast and portfolio limits passed.", max_buy)
