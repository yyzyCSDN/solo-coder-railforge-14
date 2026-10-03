from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

CENTS = Decimal('0.01')


@dataclass(frozen=True)
class ServiceLevel:
    """Customer service grade: SLA grace window and quality-credit terms."""
    level_id: str
    tier: int
    grace_minutes: float
    credit_per_minute: Decimal
    credit_cap: Decimal | None = None
    priority: int = 0


def credit_amount(delay_minutes: float, level: ServiceLevel) -> Decimal:
    """Payable quality credit for a fused delay, quantized to whole cents."""
    if level.grace_minutes < 0 or level.credit_per_minute < 0 or (
            level.credit_cap is not None and level.credit_cap < 0):
        raise ValueError('invalid service level')
    billable = max(0.0, float(delay_minutes) - float(level.grace_minutes))
    amount = level.credit_per_minute * Decimal(str(billable))
    if level.credit_cap is not None:
        amount = min(amount, level.credit_cap)
    return amount.quantize(CENTS, rounding=ROUND_HALF_UP)


def select(levels: list[ServiceLevel], tier: int | None = None,
           level_id: str | None = None) -> ServiceLevel:
    """Resolve the applicable service level deterministically."""
    rows = [l for l in levels
            if (tier is None or l.tier == tier) and (level_id is None or l.level_id == level_id)]
    if not rows:
        raise ValueError('no matching service level')
    return max(rows, key=lambda l: (l.priority, l.tier, l.level_id))
