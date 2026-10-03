from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from hashlib import sha256
from typing import Mapping

from .ledger import LedgerEntry, validate_new

GENESIS = 'GENESIS'


@dataclass(frozen=True)
class CompensationEvent:
    """Append-only accounting fact. Deterministic and replayable."""
    sequence: int
    claim_id: str
    kind: str  # settle | revise
    basis_revision: int
    delay_minutes: float
    at: str
    zone: str
    total_hours: str
    detention_lines: tuple[tuple[str, str, str], ...]  # (local date, billable hours, amount)
    credit: str
    entries: tuple[LedgerEntry, ...]
    previous_hash: str
    digest: str


@dataclass(frozen=True)
class ClaimState:
    basis_revision: int = 0
    delay_minutes: float = 0.0
    detention_refs: tuple[str, ...] = ()
    credit_ref: str | None = None


@dataclass(frozen=True)
class JournalState:
    head: int = 0
    last_hash: str = GENESIS
    claims: Mapping[str, ClaimState] = field(default_factory=dict)
    entries: tuple[LedgerEntry, ...] = ()


def canonical(event: CompensationEvent) -> str:
    body = {
        'sequence': event.sequence,
        'claim_id': event.claim_id,
        'kind': event.kind,
        'basis_revision': event.basis_revision,
        'delay_minutes': event.delay_minutes,
        'at': event.at,
        'zone': event.zone,
        'total_hours': event.total_hours,
        'detention_lines': list(event.detention_lines),
        'credit': event.credit,
        'entries': [
            {'reference': e.reference, 'account': e.account, 'category': e.category,
             'amount': str(e.amount), 'reverses': e.reverses}
            for e in event.entries
        ],
        'previous': event.previous_hash,
    }
    return json.dumps(body, sort_keys=True, separators=(',', ':'), default=str)


def digest_for(event: CompensationEvent) -> str:
    return sha256(canonical(event).encode('utf-8')).hexdigest()


def make_event(sequence: int, claim_id: str, kind: str, basis_revision: int,
               delay_minutes: float, at: str, zone: str, total_hours: str,
               detention_lines: tuple[tuple[str, str, str], ...], credit: str,
               entries: tuple[LedgerEntry, ...],
               previous_hash: str) -> CompensationEvent:
    event = CompensationEvent(sequence, claim_id, kind, basis_revision,
                              delay_minutes, at, zone, total_hours,
                              detention_lines, credit, entries, previous_hash, '')
    return CompensationEvent(event.sequence, event.claim_id, event.kind,
                             event.basis_revision, event.delay_minutes, event.at,
                             event.zone, event.total_hours, event.detention_lines,
                             event.credit, event.entries, event.previous_hash,
                             digest_for(event))


def _live(entries):
    reversed_refs = {e.reverses for e in entries if e.reverses is not None}
    return [e for e in entries
            if e.reverses is None and e.reference not in reversed_refs]


def apply(state: JournalState, event: CompensationEvent) -> JournalState:
    """Pure fold of one event onto the journal state.

    Enforces gap-free sequences, hash linkage, the no-double-pay rule (a
    revised observation must strictly advance the booked revision), and amount
    conservation: on revision every prior live posting is negated exactly once
    before the new basis posts, so event entries net to the true delta.
    """
    if event.sequence != state.head + 1:
        raise ValueError('journal sequence gap')
    if event.previous_hash != state.last_hash:
        raise ValueError('journal chain broken')
    if event.digest != digest_for(event):
        raise ValueError('event digest mismatch')
    if event.kind not in ('settle', 'revise'):
        raise ValueError('unknown event kind')

    prior = state.claims.get(event.claim_id)
    if event.kind == 'settle':
        if prior is not None:
            raise ValueError('claim already settled')
        expected_revision = 0
    else:
        if prior is None:
            raise ValueError('cannot revise unknown claim')
        expected_revision = prior.basis_revision
    if event.basis_revision <= expected_revision:
        # Late/out-of-order observation revision: must never pay twice.
        raise ValueError('stale observation revision')

    entries = list(state.entries)
    for entry in event.entries:
        validate_new(entries, entry)
        entries.append(entry)

    claim_prefix = event.claim_id + ':'
    prior_live = [e for e in _live(state.entries)
                  if e.reference.startswith(claim_prefix)]

    reversals = [e for e in event.entries if e.reverses is not None]
    postings = [e for e in event.entries if e.reverses is None]
    if event.kind == 'settle':
        if reversals or prior_live:
            raise ValueError('settlement must not reverse prior postings')
    else:
        if {r.reverses for r in reversals} != {e.reference for e in prior_live}:
            raise ValueError('revision must reverse every prior live posting')

    expected_postings: dict[str, Decimal] = {}
    detention_total = Decimal(0)
    for day, hours, amount in event.detention_lines:
        if Decimal(hours) < 0 or Decimal(amount) < 0:
            raise ValueError('detention line must be non-negative')
        ref = f'{event.claim_id}:det:{event.basis_revision}:{day}'
        expected_postings[ref] = Decimal(amount)
        detention_total += Decimal(amount)
    credit = Decimal(event.credit)
    if credit < 0:
        raise ValueError('credit must be non-negative')
    if credit > 0:
        expected_postings[f'{event.claim_id}:credit:{event.basis_revision}'] = -credit

    actual_postings = {e.reference: e.amount for e in postings}
    if actual_postings != expected_postings:
        raise ValueError('postings do not match the billed detention and credit')

    # Conservation: signed delta == new live total minus prior live total.
    prior_net = sum((e.amount for e in prior_live), Decimal(0))
    delta = sum((e.amount for e in event.entries), Decimal(0))
    if delta != detention_total - credit - prior_net:
        raise ValueError('amount conservation violated')

    detention_refs = tuple(
        f'{event.claim_id}:det:{event.basis_revision}:{day}'
        for day, _, _ in event.detention_lines)
    credit_ref = (f'{event.claim_id}:credit:{event.basis_revision}'
                  if credit > 0 else None)
    claims = dict(state.claims)
    claims[event.claim_id] = ClaimState(event.basis_revision, event.delay_minutes,
                                        detention_refs, credit_ref)
    return JournalState(event.sequence, event.digest, claims, tuple(entries))


def replay(events: list[CompensationEvent]) -> JournalState:
    state = JournalState()
    for event in sorted(events, key=lambda e: e.sequence):
        state = apply(state, event)
    return state


def verify(events: list[CompensationEvent]) -> bool:
    try:
        replay(events)
    except (ValueError, KeyError):
        return False
    return True
