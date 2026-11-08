"""Small client for Alpaca Paper orders and IEX daily market data."""

import json
import os
from datetime import date
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

PAPER_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"


class AlpacaError(Exception):
    def __init__(self, status: int | None):
        self.status = status
        message = f"Alpaca returned HTTP {status}." if status else "Alpaca could not be reached."
        super().__init__(message)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class AlpacaPaper:
    def __init__(self, key_id: str, secret: str):
        self._headers = {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret,
            "Accept": "application/json",
        }
        self._opener = build_opener(_NoRedirect())

    @classmethod
    def from_env(cls) -> "AlpacaPaper | None":
        key = os.getenv("ALPACA_PAPER_KEY_ID", "").strip()
        secret = os.getenv("ALPACA_PAPER_SECRET_KEY", "").strip()
        return cls(key, secret) if key and secret else None

    def _request(self, path, *, origin=PAPER_URL, params=None, method="GET", body=None):
        url = origin + path
        if params:
            url += "?" + urlencode(params)
        headers = dict(self._headers)
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        try:
            with self._opener.open(Request(url, data, headers, method=method), timeout=8) as response:
                payload = response.read()
                return json.loads(payload) if payload else None
        except HTTPError as error:
            raise AlpacaError(error.code) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise AlpacaError(None) from None

    def clock(self):
        return self._request("/v2/clock")

    def account(self):
        return self._request("/v2/account")

    def positions(self):
        return self._request("/v2/positions")

    def orders(self, status="all", limit=20):
        return self._request("/v2/orders", params={"status": status, "limit": limit})

    def asset(self, symbol: str):
        return self._request("/v2/assets/" + quote(symbol, safe=""))

    def calendar(self, start: date, end: date):
        return self._request("/v2/calendar", params={"start": start, "end": end})

    def bars(self, symbols: tuple[str, ...], start: date, end: date) -> dict:
        params = {
            "symbols": ",".join(symbols),
            "timeframe": "1Day",
            "start": start.isoformat() + "T00:00:00Z",
            "end": end.isoformat() + "T00:00:00Z",
            "adjustment": "all",
            "feed": "iex",
            "limit": 10000,
        }
        by_symbol = {}
        tokens = set()
        while True:
            page = self._request("/v2/stocks/bars", origin=DATA_URL, params=params)
            for symbol, bars in (page.get("bars") or {}).items():
                by_symbol.setdefault(symbol, []).extend(bars)
            token = page.get("next_page_token")
            if not token:
                return by_symbol
            if token in tokens or len(tokens) >= 8:
                raise AlpacaError(None)
            tokens.add(token)
            params["page_token"] = token

    def find_order(self, client_id: str):
        try:
            return self._request("/v2/orders:by_client_order_id", params={"client_order_id": client_id})
        except AlpacaError as error:
            if error.status == 404:
                return None
            raise

    def order(self, order_id: str):
        return self._request("/v2/orders/" + quote(order_id, safe=""))

    def submit(self, symbol: str, side: str, client_id: str, **size):
        return self._request(
            "/v2/orders",
            method="POST",
            body={
                "symbol": symbol,
                "side": side,
                "type": "market",
                "time_in_force": "day",
                "extended_hours": False,
                "client_order_id": client_id,
                **size,
            },
        )

    def cancel(self, order_id: str):
        self._request("/v2/orders/" + quote(order_id, safe=""), method="DELETE")
