from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal


@dataclass(frozen=True)
class ServiceLevel:
    """Customer SLA tier.

    ``credit_rate`` is the credited fraction of the quantized demurrage charge
    (for example ``Decimal('1.00')`` is a full credit).
    """

    level_id: str
    late_threshold: timedelta
    credit_rate: Decimal
    minimum_credit: Decimal = Decimal('0.00')
    maximum_credit: Decimal | None = None

    def __post_init__(self):
        if not Decimal('0') <= self.credit_rate <= Decimal('1'):
            raise ValueError('credit rate must be between 0 and 1')
        if self.minimum_credit < 0:
            raise ValueError('minimum credit must be non-negative')
        if self.maximum_credit is not None and self.maximum_credit < self.minimum_credit:
            raise ValueError('maximum credit must be at least minimum credit')
        if self.late_threshold <= timedelta(0):
            raise ValueError('late threshold must be positive')
