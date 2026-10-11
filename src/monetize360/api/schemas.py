"""Request/response models: PriceRequest (Decimal, no floats), PriceResponse, BatchRequest, ...

Money comes in as a string or a number and is turned into a Decimal from its TEXT (never through float
arithmetic); it goes out as a string with two decimals."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

MAX_BATCH = 500

# Shown pre-filled in Swagger ("Try it out" then Execute works as-is with domain = hotel).
_HOTEL = {"entity": "deluxe-101@2026-11-08", "base_price": "3000", "signals": {"occupancy": 0.85},
          "as_of": "2026-11-08T09:00:00Z"}


def _money(v: Any) -> Any:
    """Accept "3000", 3000 or 3000.5; reject true/false and non-numbers. The engine checks sign and size."""
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (str, int, float, Decimal)):
        raise ValueError("must be a number or a numeric string")
    try:
        d = Decimal(str(v).strip())
    except InvalidOperation:
        raise ValueError("must be a number or a numeric string") from None
    if not d.is_finite():
        raise ValueError("must be a finite number")
    return d


Money = Annotated[Decimal, BeforeValidator(_money)]


class PriceRequest(BaseModel):
    # extra="forbid": a typo like "base_prise" is an error, not a silent default
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [_HOTEL]})

    entity: str = Field(min_length=1, max_length=200, description="What is being priced, e.g. deluxe-101@2026-11-08")
    base_price: Money | None = Field(default=None, description='Money as a string, e.g. "3000"')
    signals: dict[str, Any] = Field(default_factory=dict)
    as_of: datetime | None = Field(default=None, description="ISO-8601 with a timezone; default = server time")
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class SimulateRequest(PriceRequest):
    """`/simulate`: nothing is audited; public signals may be set, and the max_change anchor given explicitly."""
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{**_HOTEL, "reference_price": "4500"}]})
    entity: str = Field(default="simulation", min_length=1, max_length=200)
    reference_price: Money | None = None
    pack_version: int | None = Field(default=None, ge=1)


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"items": [_HOTEL, {**_HOTEL, "entity": "deluxe-102@2026-11-08", "base_price": "2000"}]}]})
    # items are validated one by one in the route, so one malformed item doesn't fail the whole batch
    items: list[dict[str, Any]] = Field(min_length=1, max_length=MAX_BATCH)


class WaterfallRow(BaseModel):
    step: str
    kind: str
    delta: str
    after: str
    reason: str


class Bounds(BaseModel):
    lower: str
    upper: str


class SimulateResponse(BaseModel):
    price: str
    currency: str
    domain: str
    pack_version: int
    valid_until: str
    waterfall: list[WaterfallRow]
    counterfactuals: list[dict[str, Any]]
    signals_used: dict[str, dict[str, Any]]
    bounds: Bounds
    flags: list[str]


class PriceResponse(SimulateResponse):
    price_id: str


class BatchResponse(BaseModel):
    results: list[dict[str, Any]]
