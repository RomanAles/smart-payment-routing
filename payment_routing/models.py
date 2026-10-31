from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from .utils import iso, parse_time


@dataclass(frozen=True)
class AmountRange:
    min: float
    max: float

    @property
    def mid(self):
        return (self.min + self.max) / 2.0

    @property
    def halfwidth(self):
        return (self.max - self.min) / 2.0


@dataclass(frozen=True)
class Provider:
    payment_system: str
    priority: int = 1
    conversion_24h: float | None = None
    traffic_percentage: float = 0
    volume_share_pct: float | None = None
    preferred_range: AmountRange | None = None
    requests_per_minute_limit: float | None = None
    daily_turnover_min: float | None = None
    in_progress_count: int = 0
    in_progress_count_limit: int | None = None
    in_progress_amount: float = 0
    in_progress_amount_limit: float | None = None
    status: str = "active"
    limit_amount_min: float | None = None
    limit_amount_max: float | None = None
    daily_amount_limit: float | None = None
    daily_approved_amount: float = 0
    available_requisites: int | None = None
    banks: list[str] = field(default_factory=list)
    exclude_banks: bool = False
    provider_margin_pct: float | None = None
    merchant_margin_pct: float | None = None
    allow_negative_agreement: bool = False
    daily_turnover_max: float | None = None
    avg_latency_sec: int | None = None
    daily_approved_date: str | None = None
    daily_utc_offset: int = 0

    def with_updates(self, **overrides):
        return replace(self, **overrides)

    def to_dict(self):
        return {key: getattr(self, key) for key in self.__dataclass_fields__}


@dataclass(frozen=True)
class ProviderActuals:
    count_share_actual: float = 0
    volume_share_actual: float = 0
    count_actual: int = 0
    volume_actual: float = 0
    turnover_actual: float = 0
    rpm_used: int = 0
    request_times: list[datetime] | None = None

    def with_updates(self, **overrides):
        return replace(self, **overrides)

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Operation:
    operation_id: str
    amount: float
    bank: str | None = None
    created_at: datetime | str | None = None
    card_brand: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "amount", float(self.amount))
        parsed = parse_time(self.created_at)
        if self.created_at is not None and parsed is None:
            raise ValueError(f"Invalid operation time: {self.created_at}")
        object.__setattr__(self, "created_at", parsed)

    def to_dict(self):
        return {**asdict(self), "created_at": iso(self.created_at, True)}
