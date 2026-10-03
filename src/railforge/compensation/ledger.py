from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

CATEGORY_DETENTION = 'detention'
CATEGORY_SERVICE_CREDIT = 'service_credit'


@dataclass(frozen=True)
class LedgerEntry:
    """One immutable posting. Reversals carry the negated amount."""
    reference: str
    account: str
    category: str
    amount: Decimal
    reverses: str | None = None


def validate_new(entries: list[LedgerEntry], entry: LedgerEntry) -> None:
    """Validate ``entry`` against the append-only ledger prefix.

    * references are globally unique;
    * a reversal must target an earlier, live entry on the same account;
    * the reversal amount must be the exact negation, so a reversed pair
      nets to zero and total money is conserved.
    """
    by_ref = {e.reference: e for e in entries}
    if entry.reference in by_ref:
        raise ValueError('duplicate ledger reference')
    if entry.reverses is not None:
        original = by_ref.get(entry.reverses)
        if original is None:
            raise ValueError('orphan reversal')
        if original.reverses is not None:
            raise ValueError('cannot reverse a reversal')
        if original.account != entry.account:
            raise ValueError('reversal account mismatch')
        if original.amount != -entry.amount:
            raise ValueError('reversal must negate the original amount')
        if any(e.reverses == original.reference for e in entries):
            raise ValueError('entry already reversed')


def live(entries: list[LedgerEntry]) -> list[LedgerEntry]:
    """Entries still in force: originals that have no reversal, and not the
    reversal legs themselves (a reversed pair nets to zero and disappears)."""
    reversed_refs = {e.reverses for e in entries if e.reverses is not None}
    return [e for e in entries
            if e.reverses is None and e.reference not in reversed_refs]


def balance(entries: list[LedgerEntry], account: str | None = None,
            live_only: bool = False) -> Decimal:
    rows = live(entries) if live_only else entries
    return sum((e.amount for e in rows if account is None or e.account == account), Decimal(0))


def by_category(entries: list[LedgerEntry], live_only: bool = False) -> dict[str, Decimal]:
    rows = live(entries) if live_only else entries
    out: dict[str, Decimal] = {}
    for e in rows:
        out[e.category] = out.get(e.category, Decimal(0)) + e.amount
    return out
