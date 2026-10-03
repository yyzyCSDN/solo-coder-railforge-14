from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from railforge.billing.demurrage import Tariff
from railforge.core.time import UTC, utc as to_utc

CENTS = Decimal('0.01')
DAY = timedelta(days=1)


def _hours(delta: timedelta) -> Decimal:
    """Exact decimal hours, never a float (conservation-safe)."""
    sec = Decimal(delta.days * 86400 + delta.seconds)
    micro = Decimal(delta.microseconds) / Decimal(1_000_000)
    return (sec + micro) / Decimal(3600)


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def _largest_remainder(total: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """Split total into cent-sized pieces proportional to weights.

    Floor to cents, then hand leftover cents to the pieces with the largest
    discarded remainders so the pieces sum back to ``total`` exactly.
    """
    cents_total = int(total.scaleb(2))
    weight_sum = sum(weights, Decimal(0))
    if weight_sum <= 0:
        raise ValueError('weights must be positive')
    raw = [total * w / weight_sum for w in weights]
    floors = [r.to_integral_value(rounding='ROUND_FLOOR') for r in raw]
    cents = [int(f.scaleb(2)) for f in floors]
    remainder = cents_total - sum(cents)
    if remainder < 0:
        raise ValueError('floors exceed total')
    lexicons = sorted(
        range(len(weights)),
        key=lambda i: (raw[i] - floors[i], weights[i]),
        reverse=True,
    )
    for i in range(remainder):
        cents[lexicons[i % len(lexicons)]] += 1
    return [Decimal(c).scaleb(-2).quantize(CENTS) for c in cents]


@dataclass(frozen=True)
class DetentionLine:
    local_date: str
    hours: Decimal
    amount: Decimal


@dataclass(frozen=True)
class DetentionBreakdown:
    zone: str
    total_hours: Decimal
    billable_hours: Decimal
    lines: tuple[DetentionLine, ...]

    @property
    def total(self) -> Decimal:
        return sum((ln.amount for ln in self.lines), Decimal(0)).quantize(CENTS)


def _local_day_spans(arrival: datetime, release: datetime,
                     zone: ZoneInfo) -> list[tuple[str, datetime, datetime]]:
    """Split the interval at local midnight boundaries.

    Boundaries are real instants: on a spring-forward day local midnight
    itself is unambiguous (the jump happens later), so each boundary is the
    UTC instant whose wall clock is 00:00 of the next local date.
    """
    a = to_utc(arrival)
    r = to_utc(release)
    if r < a:
        raise ValueError('release before arrival')
    start = a.astimezone(zone)
    day0 = start.replace(hour=0, minute=0, second=0, microsecond=0)
    day_index = 1
    next_midnight = (day0 + DAY * day_index).astimezone(UTC)
    spans: list[tuple[str, datetime, datetime]] = []
    cursor = a
    while cursor < r:
        end = min(next_midnight, r)
        spans.append((cursor.astimezone(zone).date().isoformat(),
                      cursor.astimezone(zone), end.astimezone(zone)))
        cursor = end
        if cursor < r:
            # Each boundary is the instant of 00:00 wall clock on the next
            # local date, computed in wall-clock time so DST gaps/repeats
            # never shift it to 23:00/01:00 of the wrong day.
            day_index += 1
            next_midnight = (day0 + DAY * day_index).astimezone(UTC)
    return spans


def detention_breakdown(arrival: datetime, release: datetime, tariff: Tariff,
                        zone: str = 'UTC') -> DetentionBreakdown:
    """Cross-midnight demurrage: one line per local calendar day.

    The free-time allowance is consumed chronologically; chargeable hours are
    accumulated per local day. Daily-line amounts are quantized with a
    largest-remainder allocation so they always sum back to the unrounded
    total (amount conservation). When the tariff carries a daily cap, each day
    is capped independently and the sum of the capped, quantized lines is the
    authoritative total.
    """
    if tariff.free_hours < 0 or tariff.hourly < 0:
        raise ValueError('tariff')
    tz = ZoneInfo(zone)
    spans = _local_day_spans(arrival, release, tz)

    total_hours = _hours(to_utc(release) - to_utc(arrival))
    free_left = Decimal(str(tariff.free_hours))
    per_day: list[tuple[str, Decimal, Decimal]] = []  # date, billable hours, raw amount
    for day, s, e in spans:
        hours = _hours(e - s)
        free_used = min(free_left, hours)
        free_left -= free_used
        billable = hours - free_used
        if billable <= 0:
            continue
        raw = tariff.hourly * billable
        if tariff.daily_cap is not None:
            raw = min(raw, tariff.daily_cap)
        per_day.append((day, billable, raw))

    raw_total = sum((raw for _, _, raw in per_day), Decimal(0))
    total_quantized = _quantize(raw_total)
    if per_day and tariff.daily_cap is None:
        amounts = _largest_remainder(total_quantized, [h for _, h, _ in per_day])
    else:
        amounts = [_quantize(raw) for _, _, raw in per_day]

    lines = tuple(
        DetentionLine(day, hours, amounts[i])
        for i, (day, hours, _) in enumerate(per_day)
    )
    return DetentionBreakdown(
        zone=zone,
        total_hours=total_hours,
        billable_hours=sum((ln.hours for ln in lines), Decimal(0)),
        lines=lines,
    )
