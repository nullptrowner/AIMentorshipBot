from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

# JSON is an external/persistence boundary; domain money is always Decimal.
type Json = dict[str, Any]
MONEY = Decimal("0.000001")


def now() -> datetime:
    return datetime.now(UTC)


def money(value: str | Decimal | int) -> Decimal:
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError("Invalid money")
    if result.copy_abs() > Decimal("999999999999999999.999999"):
        raise ValueError("Money exceeds storage precision")
    return result.quantize(MONEY, rounding=ROUND_CEILING)


class BusinessError(Exception):
    """Safe Russian message that can be shown to a user."""


class JobStatus(StrEnum):
    CREATED = "created"
    AWAITING_PAYMENT = "awaiting_payment"
    QUEUED = "queued"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    EXPIRED = "expired"


TERMINAL = {"succeeded", "failed", "canceled", "expired"}


class MegapixelTier(BaseModel):
    max_mp: Decimal = Field(gt=0)
    cost: Decimal = Field(gt=0)


class Pricing(BaseModel):
    model_config = {"extra": "forbid"}
    fixed: Decimal = Field(default=Decimal(0), ge=0)
    per_second: Decimal = Field(default=Decimal(0), ge=0)
    per_output_mp: Decimal = Field(default=Decimal(0), ge=0)
    per_input_mp: Decimal = Field(default=Decimal(0), ge=0)
    safety_factor: Decimal = Field(default=Decimal(1), ge=1, le=10)
    markup_percent: Decimal = Field(ge=0, le=10000)
    source: str = Field(min_length=1, max_length=500)
    output_tiers: list[MegapixelTier] = Field(default_factory=list, max_length=20)
    first_output_mp_cost: Decimal | None = Field(default=None, ge=0)
    round_output_mp: bool = False
    round_input_mp: bool = False

    @model_validator(mode="after")
    def nonzero(self) -> "Pricing":
        if (
            self.fixed
            + self.per_second
            + self.per_output_mp
            + self.per_input_mp
            + (self.first_output_mp_cost or 0)
            <= 0
            and not self.output_tiers
        ):
            raise ValueError("At least one cost coefficient must be positive")
        if len({tier.max_mp for tier in self.output_tiers}) != len(self.output_tiers):
            raise ValueError("Megapixel tiers must be unique")
        return self

    def quote(
        self, seconds: Decimal, output_mp: Decimal, input_mp: Decimal
    ) -> tuple[Decimal, Decimal]:
        if self.round_output_mp:
            output_mp = output_mp.to_integral_value(rounding=ROUND_CEILING)
        if self.round_input_mp:
            input_mp = input_mp.to_integral_value(rounding=ROUND_CEILING)
        output_cost = self.per_output_mp * output_mp
        if self.first_output_mp_cost is not None and output_mp:
            output_cost = self.first_output_mp_cost + self.per_output_mp * max(
                Decimal(0), output_mp - 1
            )
        if self.output_tiers:
            tier = next(
                (
                    tier
                    for tier in sorted(self.output_tiers, key=lambda t: t.max_mp)
                    if tier.max_mp >= output_mp
                ),
                None,
            )
            if tier is None:
                raise BusinessError("Для этого разрешения не настроена стоимость.")
            output_cost = tier.cost
        cost = money(
            (self.fixed + self.per_second * seconds + output_cost + self.per_input_mp * input_mp)
            * self.safety_factor
        )
        price = money(cost * (1 + self.markup_percent / 100))
        return cost, price


class LimitRule(BaseModel):
    model_config = {"extra": "forbid"}
    daily_count: int = Field(ge=0, le=10000)
    max_seconds: int = Field(default=0, ge=0, le=3600)


class PlanInput(BaseModel):
    model_config = {"extra": "forbid"}
    name: str = Field(min_length=1, max_length=80)
    months: int = Field(ge=1, le=3)
    price: Decimal = Field(gt=0)
    limits: dict[str, list[LimitRule]]

    @model_validator(mode="after")
    def durations(self) -> "PlanInput":
        if (
            self.months not in (1, 3)
            or not self.limits
            or any(not rules for rules in self.limits.values())
        ):
            raise ValueError("Plans need a 1 or 3 month term and model limits")
        for rules in self.limits.values():
            if len({r.max_seconds for r in rules}) != len(rules):
                raise ValueError("Duration buckets must be unique")
        return self
