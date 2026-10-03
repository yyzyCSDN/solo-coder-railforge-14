import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from railforge.billing.demurrage import Tariff
from railforge.core.versioning import VersionConflict
from railforge.customer.claims import ClaimCase, ClaimStep
from railforge.customer.service_level import ServiceLevel
from railforge.eta.event_fusion import Observation
from railforge.api import RailForgeService
from railforge.service_quality.compensation import CompensationRequest, assess
from railforge.service_quality.ledger import CompensationCommand, ServiceQualityLedger

U = timezone.utc


class ServiceQualityCompensationTests(unittest.TestCase):
    def request(self, release=None, observations=None, claim_case=None, at=None, tariff=None):
        return CompensationRequest(
            shipment_id="SHIP-1",
            arrival=datetime(2026, 1, 1, 23, tzinfo=U),
            release=release or datetime(2026, 1, 2, 2, tzinfo=U),
            tariff=tariff or Tariff(free_hours=1, hourly=Decimal("10")),
            service_level=ServiceLevel(
                "gold",
                late_threshold=timedelta(minutes=30),
                credit_rate=Decimal("1.00"),
            ),
            observations=observations or [],
            max_lateness=timedelta(hours=1),
            claim_case=claim_case,
            claim_steps=[ClaimStep("proof", frozenset(), "customer")] if claim_case else [],
            at=at or release or datetime(2026, 1, 2, 2, tzinfo=U),
        )

    def observation(self, delay, sequence=1, event_time=None, received_at=None):
        base = datetime(2026, 1, 2, 1, 55, tzinfo=U)
        return Observation(
            "gps",
            event_time or base,
            received_at or base,
            delay,
            1.0,
            sequence,
        )

    def test_cross_midnight_charge_and_double_entry_are_conserved(self):
        ledger = ServiceQualityLedger()
        result = ledger.post(assess(self.request(observations=[self.observation(61)])))

        self.assertEqual(result.status, "posted")
        self.assertEqual(result.demurrage, Decimal("20.00"))
        self.assertEqual(result.credit, Decimal("20.00"))
        self.assertEqual(result.net_credit, Decimal("20.00"))
        self.assertEqual(len(result.transaction.postings), 4)
        self.assertEqual(result.transaction.amount, Decimal("0.00"))
        self.assertEqual(ledger.balance(), Decimal("0.00"))

    def test_cross_midnight_daily_cap_is_replayable(self):
        tariff = Tariff(free_hours=1, hourly=Decimal("10"), daily_cap=Decimal("100"))
        release = datetime(2026, 1, 3, 1, tzinfo=U)
        request = self.request(release=release, at=release, tariff=tariff,
                               observations=[self.observation(61)])
        first = assess(request)
        second = assess(request)

        ledger = ServiceQualityLedger()
        posted = ledger.post(first)
        replayed = ledger.post(second, posted.shipment_version)

        self.assertEqual(first.demurrage, Decimal("200.00"))
        self.assertEqual(replayed.status, "replayed")
        self.assertIsNone(replayed.transaction)
        self.assertEqual(ledger.balance(), Decimal("0.00"))
        self.assertEqual(ledger.shipment_balance("SHIP-1", "SHIP-1:credit-receivable"),
                         Decimal("200.00"))

    def test_late_observation_revision_does_not_pay_again(self):
        ledger = ServiceQualityLedger()
        first = ledger.post(assess(self.request(observations=[self.observation(61, sequence=1)])))
        revised = ledger.post(assess(self.request(
            observations=[self.observation(72, sequence=2)],
            at=datetime(2026, 1, 2, 3, tzinfo=U),
        )))
        positive = []
        for result in (first, revised):
            if result.transaction:
                positive.extend(
                    posting for posting in result.transaction.postings
                    if posting.category == "service.quality.credit" and posting.amount > 0
                    and posting.account.endswith("credit-receivable")
                )

        self.assertEqual(first.credit, Decimal("20.00"))
        self.assertEqual(revised.net_credit, Decimal("20.00"))
        self.assertEqual(len(positive), 1)
        self.assertEqual(
            ledger.shipment_balance("SHIP-1", "SHIP-1:credit-receivable"),
            Decimal("20.00"),
        )

    def test_late_arrival_revision_reverses_previous_credit(self):
        ledger = ServiceQualityLedger()
        ledger.post(assess(self.request(observations=[self.observation(90, sequence=1)])))
        revised = ledger.post(assess(self.request(
            observations=[self.observation(10, sequence=2)],
            at=datetime(2026, 1, 2, 3, tzinfo=U),
        )))

        self.assertEqual(revised.status, "posted")
        self.assertEqual(revised.credit, Decimal("0.00"))
        self.assertEqual(revised.net_credit, Decimal("0.00"))
        self.assertEqual(
            ledger.shipment_balance("SHIP-1", "SHIP-1:credit-receivable"),
            Decimal("0.00"),
        )
        self.assertEqual(ledger.balance(), Decimal("0.00"))
        reduction = [
            posting for posting in revised.transaction.postings
            if posting.reverses is not None
        ]
        self.assertEqual(len(reduction), 2)
        self.assertTrue(ledger.trail.verify())

    def test_claim_gate_pays_only_after_service_step_completes(self):
        ledger = ServiceQualityLedger()
        pending = ClaimCase("CASE-1", completed=frozenset())
        pending_assessment = assess(self.request(
            observations=[self.observation(61)], claim_case=pending))
        blocked = ledger.post(pending_assessment)
        complete = ClaimCase("CASE-1", completed=frozenset({"proof"}))
        paid = ledger.post(assess(self.request(
            observations=[self.observation(61)], claim_case=complete,
            at=datetime(2026, 1, 2, 3, tzinfo=U),
        )))

        self.assertEqual(blocked.credit, Decimal("0.00"))
        self.assertEqual(pending_assessment.pending_claim_steps, ("proof",))
        self.assertEqual(paid.credit, Decimal("20.00"))
        self.assertEqual(
            ledger.shipment_balance("SHIP-1", "SHIP-1:credit-receivable"),
            Decimal("20.00"),
        )

    def test_stale_accounting_version_conflict_can_be_replayed(self):
        ledger = ServiceQualityLedger()
        initial = assess(self.request(observations=[self.observation(90, sequence=1)]))
        corrected = assess(self.request(
            observations=[self.observation(10, sequence=2)],
            at=datetime(2026, 1, 2, 3, tzinfo=U),
        ))
        ledger.post(initial)

        with self.assertRaises(VersionConflict):
            ledger.post(corrected, expected_version=0)

        results, conflicts = ledger.replay([
            CompensationCommand("SHIP-1", None, lambda: initial),
            CompensationCommand("SHIP-1", 0, lambda: corrected),
        ])

        self.assertEqual(len(conflicts), 1)
        self.assertEqual([r.status for r in results], ["replayed", "posted"])
        self.assertEqual(ledger.balance(), Decimal("0.00"))
        self.assertEqual(
            ledger.shipment_balance("SHIP-1", "SHIP-1:credit-receivable"),
            Decimal("0.00"),
        )

    def test_stale_global_ledger_conflict_can_be_replayed(self):
        ledger = ServiceQualityLedger()
        initial = assess(self.request(observations=[self.observation(90, sequence=1)]))
        corrected = assess(self.request(
            observations=[self.observation(10, sequence=2)],
            at=datetime(2026, 1, 2, 3, tzinfo=U),
        ))
        ledger.post(initial)

        with self.assertRaises(VersionConflict):
            ledger.post(corrected, None, expected_ledger_version=0)

        results, conflicts = ledger.replay([
            CompensationCommand("SHIP-1", None, lambda: corrected, expected_ledger_version=0),
        ])
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(results[0].status, "posted")
        self.assertEqual(ledger.balance(), Decimal("0.00"))

    def test_workflow_publishes_compensation_and_audit_trail(self):
        service = RailForgeService()
        receipt = service.features.calculate_compensation(
            "comp-1",
            request=self.request(observations=[self.observation(61)]),
        )

        self.assertEqual(receipt.event.payload["status"], "committed")
        self.assertEqual(receipt.result["net_credit"], Decimal("20.00"))
        self.assertEqual(len(receipt.result["entries"]), 4)
        self.assertEqual(sum((entry["amount"] for entry in receipt.result["entries"]), Decimal("0.00")),
                         Decimal("0.00"))
        self.assertTrue(service.workflow().stream.verify_chain())
        self.assertTrue(service.workflow().trail.verify())
        self.assertEqual(service.workflow().service_quality_ledger.balance(), Decimal("0.00"))


if __name__ == "__main__":
    unittest.main()
