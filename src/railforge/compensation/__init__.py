"""Freight service-quality compensation auto-accounting.

Pipeline: fused ETA observations (:mod:`railforge.eta.event_fusion`) drive
cross-midnight detention billing (:mod:`.detention`) and a customer service
level credit (:mod:`.service_level`); postings land in a reversal-safe ledger
(:mod:`.ledger`) through an append-only, hash-chained, versioned accounting
journal (:mod:`.journal`), with the :class:`.QualityCompensationEngine`
committing each basis optimistically and appending to the shared audit trail.

Guarantees:

* observation revisions negate the prior basis before reposting, so a late or
  re-delivered observation revision can never pay twice;
* cross-midnight lines are split in the customer's local zone and allocated by
  largest remainder so per-day cents always sum back to the billed total;
* optimistic version conflicts are replayable (CAS + deterministic re-derivation)
  and every fold conserves money.
"""

from railforge.compensation.engine import (QualityCompensationEngine,
                                           Settlement, StaleRevision)
from railforge.compensation.detention import (DetentionBreakdown, DetentionLine,
                                              detention_breakdown)
from railforge.compensation.journal import (CompensationEvent, JournalState,
                                            ClaimState, apply, replay, verify)
from railforge.compensation.ledger import LedgerEntry
from railforge.compensation.service_level import ServiceLevel, credit_amount, select

__all__ = [
    'QualityCompensationEngine', 'Settlement', 'StaleRevision',
    'DetentionBreakdown', 'DetentionLine', 'detention_breakdown',
    'CompensationEvent', 'JournalState', 'ClaimState', 'apply', 'replay', 'verify',
    'LedgerEntry', 'ServiceLevel', 'credit_amount', 'select',
]
