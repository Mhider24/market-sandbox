"""Session-based forecasting and order control for one Alpaca Paper account."""

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from threading import Event, RLock

from .alpaca import AlpacaError, AlpacaPaper
from .forecast import MIN_CLOSES, forecast_next_close
from .policy import Candidate, check_candidate, select_candidate

POLL_SECONDS = 60
EQUITY_GROUP = "US equities"


class PaperRuleError(Exception):
    pass


@dataclass(frozen=True)
class BotSettings:
    symbols: tuple[str, ...] = ("SPY", "QQQ")
    max_order_cents: int = 5_000
    position_cap_bps: int = 2_000
    invested_cap_bps: int = 4_000


def _utc_date(clock: dict) -> date:
    return datetime.fromisoformat(clock["timestamp"]).astimezone(timezone.utc).date()


def _money_cents(value) -> int:
    return int(Decimal(str(value)) * 100)


def _public_order(order: dict) -> dict:
    fields = (
        "id", "client_order_id", "symbol", "side", "status",
        "notional", "qty", "filled_qty", "submitted_at",
    )
    return {name: order.get(name) for name in fields}


class PaperDesk:
    def __init__(self, broker: AlpacaPaper | None):
        self.broker = broker
        self.settings = BotSettings()
        self.armed = Event()
        self.wake = Event()
        self.stopped = Event()
        self.lock = RLock()
        self.last_auto_session: date | None = None
        self.last_order: dict | None = None
        self.review_result: dict | None = None
        self.message = "Automation paused."

    def _broker(self) -> AlpacaPaper:
        if self.broker is None:
            raise PaperRuleError("Set the Alpaca Paper credentials on the server first.")
        return self.broker

    def status(self) -> dict:
        return {
            "configured": self.broker is not None,
            "armed": self.armed.is_set(),
            "settings": asdict(self.settings),
            "message": self.message,
            "review": self.review_result,
            "last_order": self.last_order,
        }

    def configure(self, settings: BotSettings | None) -> None:
        if settings is None:
            self.armed.clear()
        with self.lock:
            if settings is None:
                self.armed.clear()
                self.message = "Automation paused."
            else:
                self._broker()
                self.settings = settings
                self.armed.set()
                self.message = "Automation armed for the next regular session check."
            self.wake.set()

    def run(self) -> None:
        while not self.stopped.is_set():
            self.tick()
            self.wake.wait(POLL_SECONDS)
            self.wake.clear()

    def stop(self) -> None:
        self.armed.clear()
        self.stopped.set()
        self.wake.set()

    def account_summary(self) -> dict:
        broker = self._broker()
        account = broker.account()
        return {
            "cash": account["cash"],
            "equity": account["equity"],
            "market_open": broker.clock()["is_open"],
            "positions": [
                {key: row.get(key) for key in ("symbol", "qty", "market_value")}
                for row in broker.positions()
            ],
            "orders": [_public_order(order) for order in broker.orders()],
        }

    def history(self, symbol: str) -> dict:
        broker = self._broker()
        today = _utc_date(broker.clock())
        bars = broker.bars((symbol,), today - timedelta(days=200), today).get(symbol, [])
        return {
            "symbol": symbol,
            "bars": [
                {key: bar[key] for key in ("t", "o", "h", "l", "c", "v")}
                for bar in bars
                if bar["t"][:10] < today.isoformat()
            ],
        }

    def review(self, settings: BotSettings) -> dict:
        with self.lock:
            broker = self._broker()
            clock = broker.clock()
            self.review_result = self._evaluate(settings, _utc_date(clock))
            return self.review_result

    def _evaluate(self, settings: BotSettings, today: date) -> dict:
        broker = self._broker()
        start = today - timedelta(days=390)
        sessions = sorted({
            row["date"] for row in broker.calendar(start, today) if row["date"] < today.isoformat()
        })
        if len(sessions) < MIN_CLOSES:
            raise PaperRuleError("Alpaca returned too few completed market sessions.")

        by_symbol = broker.bars(settings.symbols, start, today)
        account, positions = broker.account(), broker.positions()
        holdings = {}
        for position in positions:
            market_value = _money_cents(position["market_value"])
            if position["asset_class"] != "us_equity" or Decimal(position["qty"]) < 0 or market_value < 0:
                raise PaperRuleError("Automation requires a long-only US equity paper portfolio.")
            holdings[position["symbol"]] = market_value

        cash = max(0, min(_money_cents(account["cash"]), _money_cents(account["buying_power"])))
        assessments = []
        unavailable = {}
        for symbol in settings.symbols:
            closes = {
                bar["t"][:10]: bar["c"]
                for bar in by_symbol.get(symbol, [])
                if bar["t"][:10] < today.isoformat()
            }
            if any(session not in closes for session in sessions[-MIN_CLOSES:]):
                unavailable[symbol] = "Completed IEX daily bars are missing."
                continue
            history = [closes[session] for session in sessions if session in closes]
            forecast = forecast_next_close(history)
            candidate = Candidate(symbol, EQUITY_GROUP, forecast)
            decision = check_candidate(
                candidate,
                cash_cents=cash,
                portfolio_cents=max(0, _money_cents(account["equity"])),
                position_cents=holdings.get(symbol, 0),
                sector_cents=sum(holdings.values()),
                order_budget_cents=settings.max_order_cents,
                max_stock_bps=settings.position_cap_bps,
                max_sector_bps=settings.invested_cap_bps,
                excluded_sectors=set(),
            )
            assessments.append((candidate, decision))

        eligible = [(candidate, decision) for candidate, decision in assessments if decision.max_buy_cents >= 100]
        selected = select_candidate(eligible, set())
        proposal = None
        if selected:
            candidate, decision = selected
            proposal = {"symbol": candidate.symbol, "amount_cents": decision.max_buy_cents}
        return {
            "trade_session": today.isoformat(),
            "data_through": sessions[-1],
            "proposal": proposal,
            "candidates": [
                {
                    "symbol": candidate.symbol,
                    **asdict(candidate.forecast),
                    "reason": (
                        "Available amount is below the $1 order minimum."
                        if 0 < decision.max_buy_cents < 100
                        else decision.reason
                    ),
                }
                for candidate, decision in assessments
            ] + [{"symbol": symbol, "reason": reason} for symbol, reason in unavailable.items()],
        }

    def tick(self) -> None:
        if not self.armed.is_set():
            return
        with self.lock:
            if not self.armed.is_set():
                return
            try:
                self._trade_session()
            except (AlpacaError, PaperRuleError) as error:
                self.armed.clear()
                self.message = f"Automation paused. {error}"
            except (ArithmeticError, AttributeError, KeyError, TypeError, ValueError):
                self.armed.clear()
                self.message = "Automation paused because broker data could not be validated."

    def _trade_session(self) -> None:
        broker = self._broker()
        clock = broker.clock()
        if clock["is_open"] is not True:
            self.message = "Waiting for a regular US market session."
            return

        today = _utc_date(clock)
        if today == self.last_auto_session:
            return
        client_id = "ms-auto-" + today.isoformat()
        existing = broker.find_order(client_id)
        if existing:
            self.last_auto_session = today
            self.last_order = _public_order(existing)
            self.message = f"Paper order already recorded for {today}."
            return
        if broker.orders(status="open", limit=1):
            self.message = "Waiting for open paper orders to clear."
            return

        self.review_result = self._evaluate(self.settings, today)
        proposal = self.review_result["proposal"]
        if proposal is None:
            self.last_auto_session = today
            self.message = f"Forecast checked for {today}. No paper buy passed the gates."
            return

        if not self.armed.is_set() or self.stopped.is_set():
            return
        self._check_asset(proposal["symbol"])
        account = self._check_open(today)
        if proposal["amount_cents"] > min(_money_cents(account["cash"]), _money_cents(account["buying_power"])):
            raise PaperRuleError("Available paper cash changed before submission.")
        if not self.armed.is_set() or self.stopped.is_set():
            return
        self.last_order = self._submit(
            proposal["symbol"], "buy", client_id,
            notional=f"{Decimal(proposal['amount_cents']) / 100:.2f}",
        )
        self.last_auto_session = today
        self.message = f"Submitted an automatic Alpaca paper buy for {proposal['symbol']}."

    def _check_asset(self, symbol: str) -> None:
        asset = self._broker().asset(symbol)
        if asset["class"] != "us_equity" or not asset["tradable"] or not asset["fractionable"]:
            raise PaperRuleError("Choose a tradable, fractionable US equity.")

    def _check_open(self, today: date | None = None) -> dict:
        broker = self._broker()
        clock = broker.clock()
        if clock["is_open"] is not True or (today is not None and _utc_date(clock) != today):
            raise PaperRuleError("Paper orders are available during the regular US market session.")
        if broker.orders(status="open", limit=1):
            raise PaperRuleError("An open Alpaca paper order must clear before submitting another.")
        account = broker.account()
        if account["status"] != "ACTIVE" or account.get("trading_blocked"):
            raise PaperRuleError("Alpaca reports that paper trading is blocked.")
        return account

    def _submit(self, symbol: str, side: str, client_id: str, **size) -> dict:
        broker = self._broker()
        try:
            return _public_order(broker.submit(symbol, side, client_id, **size))
        except AlpacaError as error:
            existing = broker.find_order(client_id)
            if existing:
                return _public_order(existing)
            raise error

    def manual(self, symbol: str, side: str, client_id: str, amount_cents: int | None, quantity: Decimal | None) -> dict:
        self.configure(None)
        with self.lock:
            self.armed.clear()
            broker = self._broker()
            existing = broker.find_order(client_id)
            if existing:
                key, requested = ("notional", Decimal(amount_cents) / 100) if side == "buy" else ("qty", quantity)
                if existing["symbol"] != symbol or existing["side"] != side or Decimal(str(existing[key])) != requested:
                    raise PaperRuleError("This request ID already belongs to a different paper order.")
                return _public_order(existing)
            self._check_asset(symbol)
            account = self._check_open()
            if side == "buy":
                if amount_cents > min(_money_cents(account["cash"]), _money_cents(account["buying_power"])):
                    raise PaperRuleError("The paper buy exceeds available cash.")
                size = {"notional": f"{Decimal(amount_cents) / 100:.2f}"}
            else:
                position = next((row for row in broker.positions() if row["symbol"] == symbol), None)
                available = Decimal(str(position.get("qty_available", position["qty"]))) if position else Decimal(0)
                if quantity > available:
                    raise PaperRuleError("The paper sell exceeds available long shares.")
                size = {"qty": format(quantity, "f")}
            self.last_order = self._submit(symbol, side, client_id, **size)
            self.message = f"Manual paper {side} submitted. Automation paused."
            return self.last_order

    def cancel(self, order_id: str) -> None:
        self.configure(None)
        with self.lock:
            self.armed.clear()
            broker = self._broker()
            order = broker.order(order_id)
            if not order["client_order_id"].startswith(("ms-auto-", "ms-manual-")):
                raise PaperRuleError("Cancel this order in Alpaca Paper because it was placed outside Market Sandbox.")
            broker.cancel(order_id)
            self.message = "Paper cancellation requested. Automation paused."
