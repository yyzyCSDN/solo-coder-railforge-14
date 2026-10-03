from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from threading import RLock

from railforge.audit.trail import Trail
from railforge.billing.demurrage import Tariff
from railforge.core.time import utc as to_utc
from railforge.core.versioning import Cell, VersionConflict
from railforge.eta.event_fusion import Observation, fused_delay

from .detention import (DetentionBreakdown, DetentionLine,
                        detention_breakdown)
from .journal import (CompensationEvent, JournalState, apply, make_event,
                      replay as replay_journal)
from .ledger import (CATEGORY_DETENTION, CATEGORY_SERVICE_CREDIT,
                     LedgerEntry, balance, by_category, live as live_entries)
from .service_level import ServiceLevel, credit_amount


class StaleRevision(ValueError):
    """An observation revision older than the booked basis: never repaid."""


@dataclass(frozen=True)
class Settlement:
    claim_id: str
    kind: str
    basis_revision: int
    delay_minutes: float
    detention: DetentionBreakdown
    credit: Decimal
    entries: tuple[LedgerEntry, ...]
    version: int


def _build_entries(claim_id: str, account: str, kind: str, revision: int,
                   breakdown: DetentionBreakdown, credit: Decimal,
                   existing: list[LedgerEntry]) -> tuple[LedgerEntry, ...]:
    """Ledger delta for an initial settlement or an observation revision.

    On revision the live detention lines and credit of the prior basis are
    reversed exactly (``reference -> reference~R`` with the negated amount)
    before the new basis posts. A revision therefore replaces, never adds:
    it can never pay a second time on top of the first settlement.
    """
    entries: list[LedgerEntry] = []
    if kind == 'revise':
        live_prior = [e for e in live_entries(existing)
                      if e.reference.startswith(claim_id + ':')]
        for original in live_prior:
            entries.append(LedgerEntry(
                f'{original.reference}~R', account, original.category,
                -original.amount, original.reference))
    for line in breakdown.lines:
        entries.append(LedgerEntry(
            f'{claim_id}:det:{revision}:{line.local_date}', account,
            CATEGORY_DETENTION, line.amount))
    if credit > 0:
        entries.append(LedgerEntry(
            f'{claim_id}:credit:{revision}', account,
            CATEGORY_SERVICE_CREDIT, -credit))
    return tuple(entries)


def _breakdown_from_event(event: CompensationEvent) -> DetentionBreakdown:
    lines = tuple(
        DetentionLine(day, Decimal(hours), Decimal(amount))
        for day, hours, amount in event.detention_lines)
    return DetentionBreakdown(
        event.zone,
        Decimal(event.total_hours),
        sum((ln.hours for ln in lines), Decimal(0)),
        lines)


class QualityCompensationEngine:
    """Fuses ETA observations, bills cross-midnight detention, applies the
    customer service level, and commits an idempotent, replayable ledger.

    Optimistic concurrency is a version counter (one tick per accepted event).
    A mismatch raises :class:`VersionConflict`; the caller retries against the
    new version. Every retry re-derives from immutable inputs, so replayed
    settlements are identical and money stays conserved.
    """

    def __init__(self, trail: Trail | None = None):
        self._cell = Cell(JournalState())
        self._events: list[CompensationEvent] = []
        self.trail = trail or Trail()
        self._settlements: dict[str, Settlement] = {}
        self._lock = RLock()

    # ----- read side -------------------------------------------------------

    @property
    def version(self) -> int:
        return self._cell.snapshot().version

    def state(self) -> JournalState:
        return self._cell.snapshot().value

    def events(self) -> tuple[CompensationEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def last_settlement(self, claim_id: str) -> Settlement | None:
        return self._settlements.get(claim_id)

    def balances(self, account: str | None = None, live_only: bool = True):
        snap = self._cell.snapshot().value
        return {
            'total': balance(list(snap.entries), account, live_only),
            'by_category': by_category(list(snap.entries), live_only),
        }

    # ----- write side ------------------------------------------------------

    def settle(self, claim_id: str, account: str,
               observations: list[Observation], max_lateness,
               arrival: datetime, release: datetime, tariff: Tariff,
               level: ServiceLevel, basis_revision: int, at: datetime,
               zone: str = 'UTC', expected_version: int | None = None) -> Settlement:
        # Pure derivation first: nothing mutates until the CAS succeeds.
        delay = fused_delay(observations, max_lateness)
        breakdown = detention_breakdown(arrival, release, tariff, zone)
        credit = credit_amount(delay, level)

        with self._lock:
            snap = self._cell.snapshot()
            if expected_version is not None and expected_version != snap.version:
                raise VersionConflict(
                    f'expected={expected_version} actual={snap.version}')

            prior = snap.value.claims.get(claim_id)
            if prior is not None and basis_revision == prior.basis_revision:
                # Redelivery of the same observation basis: idempotent replay,
                # no new posting, no second payout.
                if abs(delay - prior.delay_minutes) > 1e-9:
                    raise ValueError('revision number reused with a different basis')
                return self._settlements[claim_id]
            if prior is not None and basis_revision < prior.basis_revision:
                raise StaleRevision(
                    f'revision {basis_revision} older than booked {prior.basis_revision}')
            kind = 'settle' if prior is None else 'revise'

            entries = _build_entries(claim_id, account, kind, basis_revision,
                                     breakdown, credit, list(snap.value.entries))
            det_lines = tuple(
                (ln.local_date, str(ln.hours), str(ln.amount))
                for ln in breakdown.lines)
            event = make_event(snap.version + 1, claim_id, kind, basis_revision,
                               delay, to_utc(at).isoformat(), zone,
                               str(breakdown.total_hours), det_lines,
                               str(credit), entries, snap.value.last_hash)
            next_state = apply(snap.value, event)  # pure validation + fold

            result = self._cell.compare_and_set(snap.version, next_state)
            self._events.append(event)
            self.trail.append('compensation-engine',
                              'compensation.settled' if kind == 'settle'
                              else 'compensation.revised',
                              claim_id, at)
            settlement = Settlement(claim_id, kind, basis_revision, delay,
                                    breakdown, credit, entries, result.version)
            self._settlements[claim_id] = settlement
            return settlement

    def settle_with_retry(self, claim_id: str, *args, retries: int = 5, **kwargs) -> Settlement:
        """CAS loop: on version conflict, re-read and re-derive deterministically."""
        attempt = 0
        while True:
            kwargs['expected_version'] = self.version
            try:
                return self.settle(claim_id, *args, **kwargs)
            except VersionConflict:
                attempt += 1
                if attempt > retries:
                    raise

    # ----- replay / audit --------------------------------------------------

    @classmethod
    def replay(cls, events: list[CompensationEvent], trail: Trail | None = None
               ) -> 'QualityCompensationEngine':
        """Rebuild an engine from its event journal; the state folds identically."""
        engine = cls(trail)
        state = JournalState()
        with engine._lock:
            for event in sorted(events, key=lambda e: e.sequence):
                state = apply(state, event)
                engine._cell.compare_and_set(state.head - 1, state)
                engine._events.append(event)
                engine.trail.append('compensation-engine',
                                    'compensation.settled' if event.kind == 'settle'
                                    else 'compensation.revised',
                                    event.claim_id, datetime.fromisoformat(event.at))
                engine._settlements[event.claim_id] = Settlement(
                    event.claim_id, event.kind, event.basis_revision,
                    event.delay_minutes, _breakdown_from_event(event),
                    Decimal(event.credit), event.entries, state.head)
        return engine

    def verify(self) -> bool:
        """Journal replays to the live state and the audit trail is intact."""
        with self._lock:
            try:
                state = replay_journal(list(self._events))
            except ValueError:
                return False
            if state.head != len(self._events) or state != self.state():
                return False
            # Each reversal targets a still-live original, exactly once.
            seen: set[str] = set()
            for e in state.entries:
                if e.reverses is not None:
                    if e.reverses in seen:
                        return False
                    seen.add(e.reverses)
            return self.trail.verify()
