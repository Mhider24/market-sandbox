"""Session-based forecasting and order control for one Alpaca Paper account."""

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from threading import Event, RLock

from .alpaca import AlpacaError, AlpacaPaper
from .forecast import BPS_PER_UNIT, MIN_CLOSES, Forecast, forecast_next_close
from .paper_audit import AuditUnavailable, PaperAudit
from .policy import MIN_FORECAST_BPS, Candidate, check_candidate, concentration_room_cents, select_candidate

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
    manage_exits: bool = False
    stop_loss_bps: int = 500
    take_profit_bps: int = 1_000


def _utc_date(clock: dict) -> date:
    return datetime.fromisoformat(clock["timestamp"]).astimezone(timezone.utc).date()


def _money_cents(value) -> int:
    return int(Decimal(str(value)) * 100)


def _available_shares(position: dict) -> Decimal:
    return min(Decimal(str(position["qty"])), Decimal(str(position.get("qty_available", position["qty"]))))


def _long_positions(positions: list[dict]) -> dict[str, dict]:
    result = {}
    for position in positions:
        if (
            position["asset_class"] != "us_equity"
            or Decimal(position["qty"]) < 0
            or _money_cents(position["market_value"]) < 0
        ):
            raise PaperRuleError("Automation requires a long-only US equity paper portfolio.")
        result[position["symbol"]] = position
    return result


def _exit_signal(
    settings: BotSettings, position: dict | None, forecast: Forecast | None
) -> tuple[int, str] | None:
    if not settings.manage_exits or position is None or _available_shares(position) <= 0:
        return None
    plpc = position.get("unrealized_plpc")
    if plpc is not None:
        return_bps = Decimal(str(plpc)) * BPS_PER_UNIT
        if return_bps <= -settings.stop_loss_bps:
            return (0, "Paper position reached the configured loss threshold.")
        if return_bps >= settings.take_profit_bps:
            return (1, "Paper position reached the configured gain threshold.")
    if forecast and forecast.beats_baseline and forecast.next_log_return_bps <= -MIN_FORECAST_BPS:
        return (2, "Negative forecast cleared the baseline gate.")
    return None


def _public_order(order: dict) -> dict:
    fields = (
        "id", "client_order_id", "symbol", "side", "status",
        "notional", "qty", "filled_qty", "filled_avg_price", "submitted_at",
    )
    return {name: order.get(name) for name in fields}


class PaperDesk:
    def __init__(self, broker: AlpacaPaper | None, audit: PaperAudit | None = None):
        self.broker = broker
        self.audit = audit
        self.settings = BotSettings()
        self.armed = Event()
        self.wake = Event()
        self.stopped = Event()
        self.lock = RLock()
        self.last_auto_session: date | None = None
        self.last_order: dict | None = None
        self.review_result: dict | None = None
        self.message = "Automation paused."

    def _record(self, event: str, summary: str, detail: dict | None = None) -> None:
        if self.audit is not None:
            self.audit.record(event, summary, detail)

    def recent_events(self) -> list[dict]:
        return self.audit.recent() if self.audit is not None else []

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
        was_armed = self.armed.is_set()
        if settings is None:
            self.armed.clear()
        with self.lock:
            if settings is None:
                self.armed.clear()
                self.message = "Automation paused."
                if was_armed:
                    try:
                        self._record("paused", self.message)
                    except AuditUnavailable:
                        self.message += " The paper journal is unavailable."
            else:
                self._broker()
                self._record("armed", "Paper automation armed.", asdict(settings))
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
            proposal = self.review_result["proposal"]
            outcome = f"{proposal['side']} {proposal['symbol']}" if proposal else "hold"
            self._record("review", f"Dry forecast result · {outcome}.", self.review_result)
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
        account, positions = broker.account(), _long_positions(broker.positions())
        holdings = {symbol: _money_cents(position["market_value"]) for symbol, position in positions.items()}

        cash = max(0, min(_money_cents(account["cash"]), _money_cents(account["buying_power"])))
        assessments = []
        candidates = []
        exits = []
        for symbol in settings.symbols:
            closes = {
                bar["t"][:10]: bar["c"]
                for bar in by_symbol.get(symbol, [])
                if bar["t"][:10] < today.isoformat()
            }
            forecast = None
            if all(session in closes for session in sessions[-MIN_CLOSES:]):
                history = [closes[session] for session in sessions if session in closes]
                forecast = forecast_next_close(history)

            position = positions.get(symbol)
            exit_signal = _exit_signal(settings, position, forecast)
            metrics = asdict(forecast) if forecast else {}
            if exit_signal:
                priority, reason = exit_signal
                quantity = format(_available_shares(position), "f")
                exits.append((priority, symbol, quantity, reason))
                candidates.append({"symbol": symbol, **metrics, "reason": reason})
                continue
            if forecast is None:
                candidates.append({"symbol": symbol, "reason": "Completed IEX daily bars are missing."})
                continue

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
            reason = decision.reason
            if 0 < decision.max_buy_cents < 100:
                reason = "Available amount is below the $1 order minimum."
            candidates.append({"symbol": symbol, **metrics, "reason": reason})

        eligible = [(candidate, decision) for candidate, decision in assessments if decision.max_buy_cents >= 100]
        selected = select_candidate(eligible, set())
        proposal = None
        if exits:
            _, symbol, quantity, reason = min(exits)
            proposal = {"side": "sell", "symbol": symbol, "quantity": quantity, "reason": reason}
        elif selected:
            candidate, decision = selected
            proposal = {"side": "buy", "symbol": candidate.symbol, "amount_cents": decision.max_buy_cents}
        return {
            "trade_session": today.isoformat(),
            "data_through": sessions[-1],
            "proposal": proposal,
            "candidates": candidates,
        }

    def tick(self) -> None:
        if not self.armed.is_set():
            return
        with self.lock:
            if not self.armed.is_set():
                return
            try:
                self._trade_session()
            except (AlpacaError, AuditUnavailable, PaperRuleError) as error:
                self.armed.clear()
                self.message = f"Automation paused. {error}"
                self._record_failure()
            except (ArithmeticError, AttributeError, KeyError, TypeError, ValueError):
                self.armed.clear()
                self.message = "Automation paused because broker data could not be validated."
                self._record_failure()

    def _record_failure(self) -> None:
        try:
            self._record("error", self.message)
        except AuditUnavailable:
            self.message += " The paper journal is unavailable."

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
            self._record("reconciled", self.message, self.last_order)
            return
        if broker.orders(status="open", limit=1):
            self.message = "Waiting for open paper orders to clear."
            return

        self.review_result = self._evaluate(self.settings, today)
        proposal = self.review_result["proposal"]
        if proposal is None:
            self.last_auto_session = today
            self.message = f"Forecast checked for {today}. Holding the paper portfolio."
            self._record("hold", self.message, self.review_result)
            return

        if not self.armed.is_set() or self.stopped.is_set():
            return
        self._check_asset(proposal["symbol"])
        account = self._check_open(today)
        size = self._confirm_automatic_size(proposal, account)
        self._record(
            "auto_intent", f"Automatic paper {proposal['side']} proposed for {proposal['symbol']}.",
            {"client_order_id": client_id, **self.review_result},
        )
        if not self.armed.is_set() or self.stopped.is_set():
            self._record("interrupted", "Automatic submission stopped before routing.", proposal)
            return
        self.last_order = self._submit(proposal["symbol"], proposal["side"], client_id, **size)
        self.last_auto_session = today
        self.message = f"Alpaca recorded an automatic paper {self.last_order['side']} for {self.last_order['symbol']}."
        self._record("auto_order", self.message, self.last_order)

    def _confirm_automatic_size(self, proposal: dict, account: dict) -> dict:
        positions = _long_positions(self._broker().positions())
        position = positions.get(proposal["symbol"])
        if proposal["side"] == "sell":
            available = _available_shares(position) if position else Decimal(0)
            quantity = Decimal(proposal["quantity"])
            if quantity > available:
                raise PaperRuleError("Available paper shares changed before submission.")
            return {"qty": proposal["quantity"]}

        equity = _money_cents(account["equity"])
        position_value = _money_cents(position["market_value"]) if position else 0
        invested = sum(_money_cents(row["market_value"]) for row in positions.values())
        limit = min(
            _money_cents(account["cash"]), _money_cents(account["buying_power"]),
            concentration_room_cents(equity, position_value, self.settings.position_cap_bps),
            concentration_room_cents(equity, invested, self.settings.invested_cap_bps),
        )
        if proposal["amount_cents"] > limit:
            raise PaperRuleError("Cash or exposure room changed before submission.")
        return {"notional": f"{Decimal(proposal['amount_cents']) / 100:.2f}"}

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
                available = _available_shares(position) if position else Decimal(0)
                if quantity > available:
                    raise PaperRuleError("The paper sell exceeds available long shares.")
                size = {"qty": format(quantity, "f")}
            self._record("manual_intent", f"Manual paper {side} requested.",
                         {"symbol": symbol, "client_order_id": client_id, **size})
            self.last_order = self._submit(symbol, side, client_id, **size)
            self.message = f"Manual paper {side} submitted. Automation paused."
            self._record("manual_order", self.message, self.last_order)
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
            try:
                self._record("cancel", self.message, _public_order(order))
            except AuditUnavailable:
                self.message += " The paper journal is unavailable."
