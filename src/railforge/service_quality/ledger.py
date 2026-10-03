from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from threading import RLock
from typing import Callable

from railforge.audit.trail import Trail
from railforge.core.versioning import VersionConflict
from railforge.service_quality.compensation import Assessment, _money

ZERO = Decimal('0.00')
DEMURRAGE_CATEGORY = 'freight.demurrage'
CREDIT_CATEGORY = 'service.quality.credit'


@dataclass(frozen=True)
class LedgerPosting:
    account: str
    reference: str
    amount: Decimal
    category: str
    reverses: str | None = None
    revision: int = 0
    corrects: str | None = None


@dataclass(frozen=True)
class LedgerTransaction:
    transaction_id: str
    shipment_id: str
    at: datetime
    postings: tuple[LedgerPosting, ...]
    revision: int
    fingerprint: str
    demurrage: Decimal
    credit: Decimal

    @property
    def amount(self) -> Decimal:
        return sum((p.amount for p in self.postings), ZERO)


@dataclass(frozen=True)
class PostingResult:
    status: str
    shipment_version: int
    ledger_version: int
    revision: int
    transaction: LedgerTransaction | None
    fingerprint: str
    demurrage: Decimal
    credit: Decimal
    net_credit: Decimal


@dataclass(frozen=True)
class LedgerSnapshot:
    ledger_version: int
    shipments: dict[str, 'ShipmentState']
    transactions: tuple[LedgerTransaction, ...]


@dataclass
class OpenPosting:
    customer_reference: str
    carrier_reference: str
    amount: Decimal
    revision: int


@dataclass
class ShipmentState:
    shipment_id: str
    version: int = 0
    revision: int = 0
    fingerprint: str | None = None
    demurrage: Decimal = ZERO
    credit: Decimal = ZERO
    open_demurrage: list[OpenPosting] = field(default_factory=list)
    open_credit: list[OpenPosting] = field(default_factory=list)


@dataclass(frozen=True)
class CompensationCommand:
    shipment_id: str
    expected_version: int | None
    assessment_factory: Callable[[], Assessment]
    expected_ledger_version: int | None = None


class ServiceQualityLedger:
    """Replay-safe double-entry ledger for demurrage and quality credits."""

    def __init__(self, trail: Trail | None = None):
        self.trail = trail or Trail()
        self.ledger_version = 0
        self._shipments: dict[str, ShipmentState] = {}
        self._references: set[str] = set()
        self._transactions: list[LedgerTransaction] = []
        self._lock = RLock()

    def snapshot(self) -> LedgerSnapshot:
        with self._lock:
            shipments = {
                key: replace(value,
                             open_demurrage=list(value.open_demurrage),
                             open_credit=list(value.open_credit))
                for key, value in self._shipments.items()
            }
            return LedgerSnapshot(self.ledger_version, shipments, tuple(self._transactions))

    def state(self, shipment_id: str) -> ShipmentState:
        with self._lock:
            current = self._shipments.get(shipment_id)
            if current is None:
                return ShipmentState(shipment_id)
            return replace(current,
                           open_demurrage=list(current.open_demurrage),
                           open_credit=list(current.open_credit))

    def balance(self, account: str | None = None) -> Decimal:
        with self._lock:
            total = ZERO
            for tx in self._transactions:
                for posting in tx.postings:
                    if account is None or posting.account == account:
                        total += posting.amount
            return _money(total)

    def shipment_balance(self, shipment_id: str, account: str | None = None) -> Decimal:
        with self._lock:
            total = ZERO
            for tx in self._transactions:
                if tx.shipment_id != shipment_id:
                    continue
                for posting in tx.postings:
                    if account is None or posting.account == account:
                        total += posting.amount
            return _money(total)

    def entries(self, shipment_id: str | None = None) -> tuple[LedgerPosting, ...]:
        with self._lock:
            return tuple(
                posting
                for tx in self._transactions
                if shipment_id is None or tx.shipment_id == shipment_id
                for posting in tx.postings
            )

    def replay(self, commands: list[CompensationCommand]):
        """Replay a command stream, including a stale-version retry.

        An initial ``VersionConflict`` is recorded but does not abort replay.
        The command's next attempt supplies the current version and produces
        exactly one set of correcting postings.
        """
        results = []
        conflicts = []
        for command in commands:
            try:
                results.append(self.post(
                    command.assessment_factory(),
                    command.expected_version,
                    command.expected_ledger_version,
                ))
            except VersionConflict as exc:
                conflicts.append(exc)
                results.append(self.post(
                    command.assessment_factory(),
                    self.state(command.shipment_id).version,
                    self.ledger_version,
                ))
        return results, conflicts

    def post(self, assessment: Assessment, expected_version: int | None = None,
             expected_ledger_version: int | None = None) -> PostingResult:
        with self._lock:
            if expected_ledger_version is not None and expected_ledger_version != self.ledger_version:
                raise VersionConflict(
                    f'ledger expected={expected_ledger_version} actual={self.ledger_version}'
                )
            state = self._shipments.setdefault(assessment.shipment_id,
                                               ShipmentState(assessment.shipment_id))
            if expected_version is not None and expected_version != state.version:
                raise VersionConflict(
                    f'shipment={assessment.shipment_id} expected={expected_version} actual={state.version}'
                )

            same = (
                state.fingerprint == assessment.fingerprint
                and state.demurrage == assessment.demurrage
                and state.credit == assessment.credit
            )
            if same:
                return PostingResult(
                    'replayed', state.version, self.ledger_version, state.revision, None,
                    assessment.fingerprint, assessment.demurrage, assessment.credit, state.credit,
                )

            revision = state.revision + 1
            postings: list[LedgerPosting] = []
            demurrage_delta = _money(assessment.demurrage - state.demurrage)
            credit_delta = _money(assessment.credit - state.credit)
            if demurrage_delta:
                postings.extend(self._adjust(
                    state, state.open_demurrage, demurrage_delta, DEMURRAGE_CATEGORY,
                    revision, 'demurrage-payable',
                ))
            if credit_delta:
                postings.extend(self._adjust(
                    state, state.open_credit, credit_delta, CREDIT_CATEGORY,
                    revision, 'credit-receivable',
                ))

            if not postings:
                audit_subject = f'{assessment.shipment_id}:observation:{revision}'
                self.trail.append(
                    'service-quality-ledger',
                    'service_quality.compensation.observed',
                    audit_subject,
                    assessment.at,
                )
                state.fingerprint = assessment.fingerprint
                state.demurrage = assessment.demurrage
                state.credit = assessment.credit
                state.revision = revision
                state.version += 1
                return PostingResult(
                    'observed', state.version, self.ledger_version, revision, None,
                    assessment.fingerprint, assessment.demurrage, assessment.credit, state.credit,
                )

            assert sum((p.amount for p in postings), ZERO) == ZERO
            tx_id = (f'{assessment.shipment_id}:{revision}'
                     if revision > 1 else f'{assessment.shipment_id}:initial')
            transaction = LedgerTransaction(
                transaction_id=tx_id,
                shipment_id=assessment.shipment_id,
                at=assessment.at,
                postings=tuple(postings),
                revision=revision,
                fingerprint=assessment.fingerprint,
                demurrage=assessment.demurrage,
                credit=assessment.credit,
            )
            self.trail.append(
                'service-quality-ledger',
                'service_quality.compensation.posted',
                tx_id,
                assessment.at,
            )
            state.fingerprint = assessment.fingerprint
            state.demurrage = assessment.demurrage
            state.credit = assessment.credit
            state.revision = revision
            state.version += 1
            self._transactions.append(transaction)
            self.ledger_version += 1
            return PostingResult(
                'posted', state.version, self.ledger_version, revision, transaction,
                assessment.fingerprint, assessment.demurrage, assessment.credit, assessment.credit,
            )

    def _adjust(self, state: ShipmentState, open_rows: list[OpenPosting], delta: Decimal,
                category: str, revision: int, account_suffix: str) -> list[LedgerPosting]:
        tx = f'{state.shipment_id}:{revision}'
        root = f'{state.shipment_id}:{category}'
        if delta > 0:
            customer_ref = f'{tx}:{category}:increase'
            carrier_ref = f'{tx}:{category}:increase:contra'
            self._require_unique(customer_ref, carrier_ref)
            open_rows.append(OpenPosting(customer_ref, carrier_ref, delta, revision))
            return [
                LedgerPosting(f'{state.shipment_id}:{account_suffix}', customer_ref, delta,
                              category, revision=revision, corrects=root),
                LedgerPosting(f'{state.shipment_id}:carrier', carrier_ref, -delta,
                              category, revision=revision, corrects=root),
            ]

        amount = -delta
        postings: list[LedgerPosting] = []
        index = 0
        while amount > ZERO:
            if not open_rows:
                raise ValueError('insufficient open posting for compensation reversal')
            row = open_rows[0]
            used = min(amount, row.amount)
            reverse_customer = f'{tx}:{category}:reduction:{index}'
            reverse_carrier = f'{reverse_customer}:contra'
            self._require_unique(reverse_customer, reverse_carrier)
            postings.append(LedgerPosting(
                f'{state.shipment_id}:{account_suffix}', reverse_customer, -used, category,
                reverses=row.customer_reference, revision=revision, corrects=root,
            ))
            postings.append(LedgerPosting(
                f'{state.shipment_id}:carrier', reverse_carrier, used, category,
                reverses=row.carrier_reference, revision=revision, corrects=root,
            ))
            row.amount = _money(row.amount - used)
            amount = _money(amount - used)
            index += 1
            if row.amount == ZERO:
                open_rows.pop(0)
        return postings

    def _require_unique(self, *references: str):
        for reference in references:
            if reference in self._references:
                raise ValueError(f'duplicate ledger reference {reference}')
            self._references.add(reference)
