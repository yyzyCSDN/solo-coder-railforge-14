from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from json import dumps

from railforge.billing.demurrage import Tariff, charge as demurrage_charge
from railforge.customer.claims import ClaimCase, finished as claim_finished
from railforge.customer.service_level import ServiceLevel
from railforge.eta.event_fusion import Observation, fused_delay

CENTS = Decimal('0.01')
UTC = timezone.utc


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('timezone-aware datetime required')
    return value.astimezone(UTC)


def observation_fingerprint(rows: list[Observation], max_lateness: timedelta) -> str:
    """Stable fingerprint for the observations eligible for an ETA fusion."""
    eligible: list[Observation] = []
    if rows:
        latest = max(r.event_time.astimezone(UTC) for r in rows)
        cut = latest - max_lateness
        seen: dict[str, Observation] = {}
        for row in rows:
            event_time = row.event_time.astimezone(UTC)
            if event_time < cut or not 0 < row.quality <= 1:
                continue
            previous = seen.get(row.source)
            if previous is None or (event_time, row.sequence) > (
                previous.event_time.astimezone(UTC),
                previous.sequence,
            ):
                seen[row.source] = row
        eligible = list(seen.values())

    payload = {
        'max_lateness_minutes': max_lateness.total_seconds() / 60,
        'observations': [
            {
                'source': row.source,
                'event_time': row.event_time.astimezone(UTC).isoformat(),
                'sequence': row.sequence,
                'delay_minutes': row.delay_minutes,
                'quality': row.quality,
            }
            for row in sorted(eligible, key=lambda r: r.source)
        ],
    }
    raw = dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return sha256(raw).hexdigest()


@dataclass(frozen=True)
class CompensationRequest:
    shipment_id: str
    arrival: datetime
    release: datetime
    tariff: Tariff
    service_level: ServiceLevel
    observations: list[Observation] = field(default_factory=list)
    max_lateness: timedelta = timedelta(hours=1)
    claim_case: ClaimCase | None = None
    claim_steps: list = field(default_factory=list)
    at: datetime | None = None


@dataclass(frozen=True)
class Assessment:
    shipment_id: str
    demurrage: Decimal
    fused_delay_minutes: Decimal
    late: bool
    eligible: bool
    pending_claim_steps: tuple[str, ...]
    credit: Decimal
    fingerprint: str
    at: datetime

    @property
    def claim_ready(self) -> bool:
        return not self.pending_claim_steps


def assess(request: CompensationRequest) -> Assessment:
    """Calculate the desired *net* demurrage credit state for a shipment.

    Revisions call this same pure function. The ledger turns differences into
    correcting double-entry postings, rather than paying a second credit.
    """
    at = _utc(request.at or request.release)
    demurrage = _money(demurrage_charge(request.arrival, request.release, request.tariff))
    delay = fused_delay(request.observations, request.max_lateness)
    delay_decimal = Decimal(str(delay)).quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)
    late = delay_decimal > Decimal(str(request.service_level.late_threshold.total_seconds() / 60))

    pending: tuple[str, ...] = ()
    if request.claim_case is not None and not request.claim_case.cancelled:
        if not claim_finished(request.claim_case, request.claim_steps):
            pending = tuple(
                sorted(
                    step.step_id
                    for step in request.claim_steps
                    if not step.optional and step.step_id not in request.claim_case.completed
                )
            )

    eligible = late and not pending and not (request.claim_case is not None and request.claim_case.cancelled)
    if eligible:
        credit = _money(demurrage * request.service_level.credit_rate)
        credit = max(credit, _money(request.service_level.minimum_credit))
        if request.service_level.maximum_credit is not None:
            credit = min(credit, _money(request.service_level.maximum_credit))
        credit = min(credit, demurrage)
    else:
        credit = Decimal('0.00')

    return Assessment(
        shipment_id=request.shipment_id,
        demurrage=demurrage,
        fused_delay_minutes=delay_decimal,
        late=late,
        eligible=eligible,
        pending_claim_steps=pending,
        credit=credit,
        fingerprint=observation_fingerprint(request.observations, request.max_lateness),
        at=at,
    )
