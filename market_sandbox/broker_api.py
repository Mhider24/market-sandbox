"""Local-only FastAPI routes for the Alpaca Paper desk."""

from contextlib import asynccontextmanager, contextmanager
from decimal import Decimal
from pathlib import Path
from threading import Thread
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .alpaca import AlpacaError, AlpacaPaper
from .paper_audit import AuditUnavailable, PaperAudit
from .paper_bot import BotSettings, PaperDesk, PaperRuleError

router = APIRouter(prefix="/alpaca", tags=["Alpaca Paper"])
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
StockSymbol = Annotated[
    str, StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Za-z][A-Za-z0-9.-]{0,11}$")
]


@asynccontextmanager
async def paper_desk_lifespan(app):
    broker = AlpacaPaper.from_env()
    audit = None
    if broker is not None:
        audit = PaperAudit(Path.home() / ".market-sandbox" / "paper-audit.sqlite3", broker.audit_scope)
    desk = PaperDesk(broker, audit)
    app.state.paper_desk = desk
    worker = None
    if desk.broker is not None:
        worker = Thread(target=desk.run, name="alpaca-paper-bot", daemon=True)
        worker.start()
    try:
        yield
    finally:
        desk.stop()
        if worker:
            worker.join(timeout=10)


class SettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbols: Annotated[list[StockSymbol], Field(min_length=1, max_length=8)]
    max_order_cents: Annotated[int, Field(ge=100, le=100_000, strict=True)] = 5_000
    position_cap_bps: Annotated[int, Field(ge=1, le=10_000, strict=True)] = 2_000
    invested_cap_bps: Annotated[int, Field(ge=1, le=10_000, strict=True)] = 4_000
    manage_exits: Annotated[bool, Field(strict=True)] = False
    stop_loss_bps: Annotated[int, Field(ge=1, le=10_000, strict=True)] = 500
    take_profit_bps: Annotated[int, Field(ge=1, le=10_000, strict=True)] = 1_000

    @model_validator(mode="after")
    def unique_symbols(self):
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("Use each symbol once.")
        return self

    def settings(self) -> BotSettings:
        return BotSettings(**self.model_dump(exclude={"symbols"}), symbols=tuple(self.symbols))


class ManualOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: StockSymbol
    side: Literal["buy", "sell"]
    request_id: UUID
    amount_cents: Annotated[int, Field(ge=100, le=100_000_000, strict=True)] | None = None
    quantity: Annotated[Decimal, Field(gt=0, allow_inf_nan=False, max_digits=18, decimal_places=9)] | None = None

    @model_validator(mode="after")
    def size_for_side(self):
        if self.side == "buy" and (self.amount_cents is None or self.quantity is not None):
            raise ValueError("Specify a dollar amount for a paper buy.")
        if self.side == "sell" and (self.quantity is None or self.amount_cents is not None):
            raise ValueError("Specify a share quantity for a paper sell.")
        return self


def local_desk(request: Request) -> PaperDesk:
    if request.url.hostname not in LOCAL_HOSTS or request.client is None or request.client.host not in LOCAL_HOSTS:
        raise HTTPException(403, "The Alpaca desk is available on localhost.")
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403, "Use the local Market Sandbox workspace.")
    if request.method != "GET" and request.headers.get("x-market-sandbox") != "paper":
        raise HTTPException(403, "This action requires the paper desk request header.")
    return request.app.state.paper_desk


@contextmanager
def paper_errors():
    try:
        yield
    except PaperRuleError as error:
        raise HTTPException(409, str(error)) from None
    except AlpacaError as error:
        raise HTTPException(502, str(error)) from None
    except AuditUnavailable as error:
        raise HTTPException(503, str(error)) from None
    except (ArithmeticError, AttributeError, KeyError, TypeError, ValueError):
        raise HTTPException(502, "Broker data could not be validated.") from None


@router.get("/status")
def status(request: Request):
    return local_desk(request).status()


@router.get("/account")
def account(request: Request):
    desk = local_desk(request)
    with paper_errors():
        return desk.account_summary()


@router.get("/audit")
def audit(request: Request):
    desk = local_desk(request)
    with paper_errors():
        return {"events": desk.recent_events()}


@router.get("/history/{symbol}")
def history(symbol: StockSymbol, request: Request):
    desk = local_desk(request)
    with paper_errors():
        return desk.history(symbol)


@router.post("/arm")
def arm(settings: SettingsRequest, request: Request):
    desk = local_desk(request)
    with paper_errors():
        desk.configure(settings.settings())
        return desk.status()


@router.post("/pause")
def pause(request: Request):
    desk = local_desk(request)
    with paper_errors():
        desk.configure(None)
        return desk.status()


@router.post("/review")
def review(settings: SettingsRequest, request: Request):
    desk = local_desk(request)
    with paper_errors():
        return desk.review(settings.settings())


@router.post("/orders")
def manual_order(order: ManualOrder, request: Request):
    desk = local_desk(request)
    with paper_errors():
        return desk.manual(
            order.symbol, order.side, "ms-manual-" + order.request_id.hex,
            order.amount_cents, order.quantity,
        )


@router.delete("/orders/{order_id}", status_code=204)
def cancel_order(order_id: UUID, request: Request):
    desk = local_desk(request)
    with paper_errors():
        desk.cancel(str(order_id))
