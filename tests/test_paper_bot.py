import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException, Request

from market_sandbox.alpaca import AlpacaError, AlpacaPaper, DATA_URL, PAPER_URL
from market_sandbox.broker_api import SettingsRequest, local_desk
from market_sandbox.forecast import Forecast, MIN_CLOSES
from market_sandbox.paper_audit import AuditUnavailable, PaperAudit
from market_sandbox.paper_bot import BotSettings, PaperDesk, PaperRuleError

TODAY = date(2026, 11, 9)
SESSIONS = [TODAY - timedelta(days=day) for day in range(MIN_CLOSES, 0, -1)]
PASSING = Forecast(30.0, 100.0, 50.0, 80.0, 40)
FAILING = Forecast(30.0, 100.0, 90.0, 80.0, 40)
LONG_SPY = {
    "asset_class": "us_equity", "symbol": "SPY", "market_value": "100",
    "qty": "1.25", "qty_available": "0.75", "unrealized_plpc": "-0.05",
}


class FakeBroker:
    def __init__(self):
        self.is_open = True
        self.held = []
        self.cash = "1000"
        self.open_orders = []
        self.sent = {}
        self.submissions = 0
        self.lost_response = False
        self.cancelled = []

    def clock(self):
        return {"timestamp": f"{TODAY}T15:00:00Z", "is_open": self.is_open}

    def calendar(self, start, end):
        return [{"date": day.isoformat()} for day in SESSIONS + [TODAY]]

    def bars(self, symbols, start, end):
        return {
            symbol: [{"t": f"{day}T05:00:00Z", "c": 100 + index}
                     for index, day in enumerate(SESSIONS + [TODAY])]
            for symbol in symbols
        }

    def account(self):
        return {"cash": self.cash, "equity": "1000", "buying_power": "2000", "status": "ACTIVE"}

    def positions(self):
        return self.held

    def orders(self, status="all", limit=20):
        return self.open_orders if status == "open" else list(self.sent.values())

    def find_order(self, client_id):
        return self.sent.get(client_id)

    def asset(self, symbol):
        return {"class": "us_equity", "tradable": True, "fractionable": True}

    def submit(self, symbol, side, client_id, **size):
        self.submissions += 1
        order = {
            "id": str(uuid4()), "client_order_id": client_id, "symbol": symbol,
            "side": side, "status": "new", **size,
        }
        self.sent[client_id] = order
        if self.lost_response:
            raise AlpacaError(None)
        return order

    def order(self, order_id):
        return next(order for order in self.sent.values() if order["id"] == order_id)

    def cancel(self, order_id):
        self.cancelled.append(order_id)


class PaperDeskTests(unittest.TestCase):
    def setUp(self):
        self.broker = FakeBroker()
        self.desk = PaperDesk(self.broker)
        self.settings = BotSettings(("SPY",), max_order_cents=5_000)

    @patch("market_sandbox.paper_bot.forecast_next_close", return_value=PASSING)
    def test_review_uses_completed_bars_and_auto_is_once_per_session(self, forecast):
        self.desk.tick()
        self.assertEqual(self.broker.submissions, 0)
        review = self.desk.review(self.settings)
        self.assertEqual(self.broker.submissions, 0)
        self.assertEqual(review["data_through"], str(SESSIONS[-1]))
        self.assertEqual(len(forecast.call_args.args[0]), MIN_CLOSES)
        self.assertEqual(forecast.call_args.args[0][-1], 100 + MIN_CLOSES - 1)

        self.desk.configure(self.settings)
        self.desk.tick()
        self.desk.tick()
        restarted = PaperDesk(self.broker)
        restarted.configure(self.settings)
        restarted.tick()
        self.assertEqual(self.broker.submissions, 1)
        self.assertEqual(self.broker.sent[f"ms-auto-{TODAY}"]["notional"], "50.00")

    @patch("market_sandbox.paper_bot.forecast_next_close", return_value=FAILING)
    def test_failed_forecast_gate_holds(self, forecast):
        self.desk.configure(self.settings)
        self.desk.tick()
        self.assertEqual(self.broker.submissions, 0)
        self.assertIsNone(self.desk.status()["review"]["proposal"])
        self.assertIn("baseline", self.desk.status()["review"]["candidates"][0]["reason"])

    @patch("market_sandbox.paper_bot.forecast_next_close", return_value=PASSING)
    def test_market_closed_and_missing_history_hold(self, forecast):
        self.desk.configure(self.settings)
        self.broker.is_open = False
        self.desk.tick()
        forecast.assert_not_called()
        self.broker.is_open = True
        with patch.object(self.broker, "bars", return_value={"SPY": []}):
            self.desk.tick()
        self.assertEqual(self.broker.submissions, 0)
        self.assertIn("missing", self.desk.status()["review"]["candidates"][0]["reason"])

    @patch("market_sandbox.paper_bot.forecast_next_close", return_value=PASSING)
    def test_cash_and_total_equity_exposure_cap(self, forecast):
        self.broker.held = [{"asset_class": "us_equity", "symbol": "VTI", "market_value": "399", "qty": "2"}]
        self.desk.configure(self.settings)
        self.desk.tick()
        self.assertEqual(self.broker.sent[f"ms-auto-{TODAY}"]["notional"], "1.00")

    @patch("market_sandbox.paper_bot.forecast_next_close", return_value=PASSING)
    def test_opt_in_sell_takes_priority_and_uses_available_shares(self, forecast):
        self.broker.held = [LONG_SPY]
        settings = SettingsRequest(symbols=["QQQ", "SPY"], manage_exits=True).settings()
        review = self.desk.review(settings)
        self.assertEqual(review["proposal"]["side"], "sell")
        self.assertEqual(self.broker.submissions, 0)

        self.desk.configure(settings)
        self.desk.tick()
        order = self.broker.sent[f"ms-auto-{TODAY}"]
        self.assertEqual((order["symbol"], order["side"], order["qty"]), ("SPY", "sell", "0.75"))

        restarted = PaperDesk(self.broker)
        restarted.configure(settings)
        restarted.tick()
        self.assertEqual(self.broker.submissions, 1)

    @patch("market_sandbox.paper_bot.forecast_next_close")
    def test_exits_respect_opt_in_watchlist_and_validation_gate(self, forecast):
        self.broker.held = [{**LONG_SPY, "unrealized_plpc": "0.01"}]
        forecast.return_value = Forecast(-30.0, 100.0, 50.0, 80.0, 40)
        self.assertIsNone(self.desk.review(self.settings)["proposal"])
        self.assertIsNone(self.desk.review(BotSettings(("QQQ",), manage_exits=True))["proposal"])
        settings = BotSettings(("SPY",), manage_exits=True)
        self.assertIn("Negative forecast", self.desk.review(settings)["proposal"]["reason"])
        forecast.return_value = Forecast(-30.0, 100.0, 90.0, 80.0, 40)
        self.assertIsNone(self.desk.review(settings)["proposal"])

        self.broker.held = [{**LONG_SPY, "unrealized_plpc": "0.10"}]
        with patch.object(self.broker, "bars", return_value={}):
            self.assertIn("gain threshold", self.desk.review(settings)["proposal"]["reason"])

    @patch("market_sandbox.paper_bot.forecast_next_close", return_value=PASSING)
    def test_sell_pauses_if_available_shares_change(self, forecast):
        self.desk.configure(BotSettings(("SPY",), manage_exits=True))
        remaining = {**LONG_SPY, "qty_available": "0.25"}
        with patch.object(self.broker, "positions", side_effect=[[LONG_SPY], [remaining]]):
            self.desk.tick()
        self.assertEqual(self.broker.submissions, 0)
        self.assertFalse(self.desk.status()["armed"])
        self.assertIn("Available paper shares changed", self.desk.status()["message"])

    @patch("market_sandbox.paper_bot.forecast_next_close", return_value=PASSING)
    def test_audit_survives_restart_and_is_scoped_to_credentials(self, forecast):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "paper.sqlite3"
            desk = PaperDesk(self.broker, PaperAudit(path, "paper-account"))
            desk.review(self.settings)
            desk.configure(self.settings)
            desk.tick()
            events = PaperAudit(path, "paper-account").recent()
            self.assertEqual([event["event"] for event in events], ["auto_order", "auto_intent", "armed", "review"])
            self.assertEqual(events[1]["detail"]["proposal"]["amount_cents"], 5_000)
            self.assertEqual(events[0]["detail"]["status"], "new")
            self.assertEqual(PaperAudit(path, "other-account").recent(), [])

    @patch("market_sandbox.paper_bot.forecast_next_close", return_value=PASSING)
    def test_audit_failure_blocks_new_auto_orders_but_allows_cancellation(self, forecast):
        existing = self.desk.manual("SPY", "buy", "ms-manual-1", 1000, None)
        with TemporaryDirectory() as directory:
            audit = PaperAudit(Path(directory) / "paper.sqlite3", "paper-account")
            desk = PaperDesk(self.broker, audit)
            desk.configure(self.settings)
            with patch.object(audit, "record", side_effect=AuditUnavailable("Paper journal unavailable.")):
                desk.tick()
                self.assertEqual(self.broker.submissions, 1)
                self.assertFalse(desk.status()["armed"])
                desk.configure(None)
                desk.cancel(existing["id"])
            self.assertEqual(self.broker.cancelled, [existing["id"]])

    def test_manual_order_pauses_and_reconciles_a_lost_response(self):
        self.desk.configure(self.settings)
        self.broker.lost_response = True
        order = self.desk.manual("SPY", "buy", "ms-manual-1", 1000, None)
        retry = self.desk.manual("SPY", "buy", "ms-manual-1", 1000, None)
        self.assertEqual(order["id"], retry["id"])
        self.assertEqual(self.broker.submissions, 1)
        self.assertFalse(self.desk.status()["armed"])
        with self.assertRaises(PaperRuleError):
            self.desk.manual("QQQ", "buy", "ms-manual-1", 1000, None)

    def test_manual_buy_checks_cash_and_sell_checks_long_quantity(self):
        self.broker.cash = "8"
        self.broker.held = [{"symbol": "SPY", "qty": "1.25", "qty_available": "0.75"}]
        with self.assertRaisesRegex(PaperRuleError, "exceeds available cash"):
            self.desk.manual("SPY", "buy", "ms-manual-1", 1000, None)
        with self.assertRaisesRegex(PaperRuleError, "exceeds available long shares"):
            self.desk.manual("SPY", "sell", "ms-manual-2", None, Decimal("1"))
        result = self.desk.manual("SPY", "sell", "ms-manual-3", None, Decimal("0.5"))
        self.assertEqual(result["qty"], "0.5")
        self.desk.cancel(result["id"])
        self.assertEqual(self.broker.cancelled, [result["id"]])


class BrokerClientTests(unittest.TestCase):
    def test_order_uses_paper_origin_and_history_uses_iex(self):
        client = AlpacaPaper("test-key", "test-secret")
        with patch.object(client, "_request", return_value={"bars": {}}) as request:
            client.submit("SPY", "buy", "ms-auto-2026-11-09", notional="25.00")
            self.assertEqual(request.call_args.args[0], "/v2/orders")
            self.assertEqual(request.call_args.kwargs["body"]["time_in_force"], "day")
            self.assertFalse(request.call_args.kwargs["body"]["extended_hours"])
            self.assertEqual(AlpacaPaper._request.__kwdefaults__["origin"], PAPER_URL)
            client.bars(("SPY",), SESSIONS[0], TODAY)
            self.assertEqual(request.call_args.kwargs["origin"], DATA_URL)
            self.assertEqual(request.call_args.kwargs["params"]["feed"], "iex")


class PaperRouteTests(unittest.TestCase):
    def test_route_needs_local_origin_and_paper_header(self):
        from types import SimpleNamespace

        desk = PaperDesk(None)
        scope = {
            "type": "http", "method": "POST", "scheme": "http",
            "path": "/alpaca/pause", "server": ("127.0.0.1", 80),
            "client": ("127.0.0.1", 55001),
            "app": SimpleNamespace(state=SimpleNamespace(paper_desk=desk)),
        }
        for headers in ([], [(b"x-market-sandbox", b"paper"), (b"origin", b"https://another-site.example")]):
            with self.assertRaises(HTTPException) as denied:
                local_desk(Request({**scope, "headers": headers}))
            self.assertEqual(denied.exception.status_code, 403)
        headers = [(b"x-market-sandbox", b"paper"), (b"origin", b"http://127.0.0.1")]
        self.assertIs(local_desk(Request({**scope, "headers": headers})), desk)
