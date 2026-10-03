import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from railforge.api import RailForgeService
from railforge.audit.trail import Trail
from railforge.billing.demurrage import Tariff
from railforge.compensation.detention import detention_breakdown
from railforge.compensation.engine import QualityCompensationEngine, StaleRevision
from railforge.compensation.journal import replay as replay_journal
from railforge.compensation.ledger import (LedgerEntry, balance, by_category,
                                           live as live_entries, validate_new)
from railforge.compensation.service_level import ServiceLevel, credit_amount
from railforge.core.versioning import VersionConflict
from railforge.eta.event_fusion import Observation

U = timezone.utc
D = Decimal


def obs(delay, source='ops', seq=1, at=None):
    at = at or datetime(2026, 3, 1, 12, tzinfo=U)
    return Observation(source, at, at, delay, 1.0, seq)


GOLD = ServiceLevel('gold', tier=1, grace_minutes=30, credit_per_minute=D('1.50'),
                    credit_cap=D('300.00'))


class CrossMidnightTests(unittest.TestCase):
    def test_lines_split_at_local_midnight_and_sum_to_total(self):
        # 26:00 local detention in Shanghai: 20h on day 1, 6h on day 2.
        arrival = datetime(2026, 3, 1, 20, tzinfo=U)       # 2026-03-02 04:00 +08
        release = datetime(2026, 3, 2, 22, tzinfo=U)       # 2026-03-03 06:00 +08
        tariff = Tariff(free_hours=6, hourly=D('10.00'))
        b = detention_breakdown(arrival, release, tariff, zone='Asia/Shanghai')
        self.assertEqual([ln.local_date for ln in b.lines], ['2026-03-02', '2026-03-03'])
        self.assertEqual(b.lines[0].hours, D('14'))   # 20h day - 6h free time
        self.assertEqual(b.lines[1].hours, D('6'))
        # largest-remainder allocation conserves the total to the cent
        self.assertEqual(sum((ln.amount for ln in b.lines), D(0)),
                         (D('10.00') * D('20.00')).quantize(D('0.01')))
        self.assertEqual(b.total, D('200.00'))

    def test_daily_cap_applies_per_calendar_day(self):
        arrival = datetime(2026, 3, 1, 16, tzinfo=U)       # 00:00 +08 on day 2
        release = datetime(2026, 3, 3, 16, tzinfo=U)       # 48h, two full days
        tariff = Tariff(free_hours=0, hourly=D('10.00'), daily_cap=D('150.00'))
        b = detention_breakdown(arrival, release, tariff, zone='Asia/Shanghai')
        self.assertEqual(len(b.lines), 2)
        self.assertEqual([ln.amount for ln in b.lines], [D('150.00'), D('150.00')])
        self.assertEqual(b.total, D('300.00'))

    def test_dst_transition_preserves_elapsed_hours(self):
        # Europe/Berlin springs forward 02:00->03:00 on 2026-03-29.
        a = datetime(2026, 3, 28, 10, tzinfo=U)
        r = datetime(2026, 3, 29, 12, tzinfo=U)
        b = detention_breakdown(a, r, Tariff(0, D('1.37')), zone='Europe/Berlin')
        self.assertEqual(sum((ln.hours for ln in b.lines), D(0)), D('27'))
        self.assertEqual(b.lines[0].hours, D('13'))
        self.assertEqual(b.lines[1].hours, D('14'))
        # Fractional-cent total is allocated with largest remainder: exact sum.
        self.assertEqual(b.total, (D('1.37') * D('27')).quantize(D('0.01')))
        self.assertEqual(sum((ln.amount for ln in b.lines), D(0)), b.total)
        # Fall-back day 2026-10-25 03:00->02:00 gains an hour.
        b2 = detention_breakdown(datetime(2026, 10, 24, 10, tzinfo=U),
                                 datetime(2026, 10, 25, 12, tzinfo=U),
                                 Tariff(0, D('1')), zone='Europe/Berlin')
        self.assertEqual(sum((ln.hours for ln in b2.lines), D(0)), D('25'))


class LedgerTests(unittest.TestCase):
    def test_reversal_must_negate_and_can_only_happen_once(self):
        original = LedgerEntry('c:det:1:2026-03-02', 'acct', 'detention', D('100'))
        validate_new([], original)
        good = LedgerEntry('c:det:1:2026-03-02~R', 'acct', 'detention', D('-100'),
                           'c:det:1:2026-03-02')
        validate_new([original], good)
        with self.assertRaises(ValueError):
            validate_new([original], LedgerEntry('x', 'acct', 'detention', D('-99'),
                                                 'c:det:1:2026-03-02'))
        with self.assertRaises(ValueError):
            validate_new([original, good],
                         LedgerEntry('y', 'acct', 'detention', D('-100'),
                                     'c:det:1:2026-03-02'))
        with self.assertRaises(ValueError):
            validate_new([original], LedgerEntry('z', 'other', 'detention', D('-100'),
                                                 'c:det:1:2026-03-02'))

    def test_live_balance_excludes_reversed_pairs(self):
        entries = [
            LedgerEntry('a', 'x', 'detention', D('100')),
            LedgerEntry('a~R', 'x', 'detention', D('-100'), 'a'),
            LedgerEntry('b', 'x', 'detention', D('40')),
        ]
        self.assertEqual(balance(entries), D('40'))
        self.assertEqual(balance(live_entries(entries)), D('40'))
        self.assertEqual(by_category(live_entries(entries)), {'detention': D('40')})


class ServiceLevelTests(unittest.TestCase):
    def test_grace_and_cap(self):
        self.assertEqual(credit_amount(10, GOLD), D('0.00'))
        self.assertEqual(credit_amount(40, GOLD), D('15.00'))   # 10 min * 1.50
        self.assertEqual(credit_amount(1000, GOLD), D('300.00'))  # capped


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tariff = Tariff(free_hours=0, hourly=D('10.00'))
        # 40 hours across two local days: 20 + 20 -> 200 + 200 detention
        self.arrival = datetime(2026, 3, 1, 16, tzinfo=U)
        self.release = datetime(2026, 3, 3, 8, tzinfo=U)
        self.at = datetime(2026, 3, 3, 9, tzinfo=U)

    def _settle(self, engine, delay, revision, claim='C1', at=None):
        return engine.settle(claim, 'ACME', [obs(delay, seq=revision)],
                             timedelta(hours=6), self.arrival, self.release,
                             self.tariff, GOLD, revision, at or self.at,
                             zone='Asia/Shanghai')

    def test_initial_settlement_fuses_bills_and_pays_credit(self):
        engine = QualityCompensationEngine(Trail())
        s = self._settle(engine, 40, 1)
        self.assertEqual(s.kind, 'settle')
        self.assertEqual(s.detention.total, D('400.00'))
        self.assertEqual(s.credit, D('15.00'))
        bal = engine.balances()
        self.assertEqual(bal['by_category']['detention'], D('400.00'))
        self.assertEqual(bal['by_category']['service_credit'], D('-15.00'))
        self.assertTrue(engine.verify())

    def test_observation_revision_reverses_then_reposts_no_double_pay(self):
        engine = QualityCompensationEngine(Trail())
        self._settle(engine, 40, 1)
        # Revised arrival: actually 20 minutes late -> below the 30 min grace.
        s2 = self._settle(engine, 20, 2)
        self.assertEqual(s2.kind, 'revise')
        self.assertEqual(s2.credit, D('0.00'))
        # Signed (all-entry) balance nets prior basis out exactly:
        self.assertEqual(balance(list(engine.state().entries)), D('400.00'))
        # Live view only carries the new basis:
        live = live_entries(list(engine.state().entries))
        self.assertEqual(balance(live), D('400.00'))
        self.assertNotIn('service_credit', by_category(live))
        self.assertTrue(engine.verify())

    def test_same_basis_redelivery_is_idempotent(self):
        engine = QualityCompensationEngine(Trail())
        first = self._settle(engine, 40, 1)
        again = self._settle(engine, 40, 1, at=self.at + timedelta(hours=1))
        self.assertIs(first, again)
        self.assertEqual(len(engine.events()), 1)
        self.assertTrue(engine.verify())

    def test_stale_revision_is_rejected_not_paid(self):
        engine = QualityCompensationEngine(Trail())
        self._settle(engine, 40, 2)
        with self.assertRaises(StaleRevision):
            self._settle(engine, 500, 1)
        live = live_entries(list(engine.state().entries))
        self.assertEqual(by_category(live)['service_credit'], D('-15.00'))
        self.assertTrue(engine.verify())

    def test_three_revisions_keep_amount_conserved(self):
        engine = QualityCompensationEngine(Trail())
        self._settle(engine, 200, 1)   # capped credit 300
        self._settle(engine, 90, 2)    # credit 90
        self._settle(engine, 40, 3)    # credit 15
        live = live_entries(list(engine.state().entries))
        self.assertEqual(balance(live), D('400.00') - D('15.00'))
        self.assertEqual(by_category(live)['service_credit'], D('-15.00'))
        self.assertEqual(len(engine.events()), 3)
        self.assertTrue(engine.verify())

    def test_version_conflict_is_replayable_via_retry(self):
        engine = QualityCompensationEngine(Trail())
        with self.assertRaises(VersionConflict):
            engine.settle('C1', 'ACME', [obs(40)], timedelta(hours=6),
                          self.arrival, self.release, self.tariff, GOLD, 1,
                          self.at, zone='Asia/Shanghai', expected_version=99)
        s = engine.settle_with_retry('C1', 'ACME', [obs(40)], timedelta(hours=6),
                                     self.arrival, self.release, self.tariff,
                                     GOLD, 1, self.at, zone='Asia/Shanghai')
        self.assertEqual(s.version, 1)
        self.assertTrue(engine.verify())

    def test_journal_replay_rebuilds_identical_state(self):
        engine = QualityCompensationEngine(Trail())
        self._settle(engine, 40, 1)
        self._settle(engine, 20, 2)
        rebuilt = QualityCompensationEngine.replay(list(engine.events()))
        self.assertEqual(rebuilt.state(), engine.state())
        self.assertTrue(rebuilt.verify())
        s = rebuilt.last_settlement('C1')
        self.assertEqual(s.detention.total, D('400.00'))
        self.assertEqual(s.detention.total_hours, D('40'))
        self.assertEqual(s.credit, D('0.00'))
        self.assertTrue(replay_journal(list(engine.events())) == engine.state())

    def test_tampered_event_breaks_verify(self):
        engine = QualityCompensationEngine(Trail())
        self._settle(engine, 40, 1)
        bad = list(engine.events())[0]
        object.__setattr__(bad, 'credit', '999.00')
        with self.assertRaises(ValueError):
            replay_journal([bad])

    def test_interleaved_claims_only_reverse_their_own_basis(self):
        engine = QualityCompensationEngine(Trail())
        self._settle(engine, 60, 1, claim='C1')
        self._settle(engine, 20, 1, claim='C2')
        self._settle(engine, 10, 2, claim='C1')  # C1 drops under grace
        lv = live_entries(list(engine.state().entries))
        refs = {e.reference for e in lv}
        self.assertIn('C2:det:1:2026-03-02', refs)       # untouched
        self.assertIn('C1:det:2:2026-03-02', refs)       # revised basis
        self.assertNotIn('C1:det:1:2026-03-02', refs)
        self.assertEqual(balance(lv), D('800.00'))       # 400 detention x 2
        self.assertTrue(engine.verify())


class WorkflowIntegrationTests(unittest.TestCase):
    def test_feature_facade_commits_event_and_audit(self):
        service = RailForgeService()
        arrival = datetime(2026, 3, 1, 16, tzinfo=U)
        release = datetime(2026, 3, 2, 0, tzinfo=U)     # 8h, same Shanghai day
        at = datetime(2026, 3, 2, 2, tzinfo=U)
        receipt = service.features.auto_compensate(
            'op-1', 'CLM-1', 'ACME', [obs(60)], timedelta(hours=6),
            arrival, release, Tariff(0, D('10.00')), GOLD, 1, at,
            zone='Asia/Shanghai')
        self.assertEqual(receipt.event.payload['status'], 'committed')
        self.assertEqual(receipt.event.payload['control_path'], 'atomic')
        self.assertEqual(receipt.result.credit, D('45.00'))   # (60-30)*1.5
        self.assertEqual(receipt.result.detention.total, D('80.00'))
        # Idempotent replay of the same operation id
        again = service.features.auto_compensate(
            'op-1', 'CLM-1', 'ACME', [obs(60)], timedelta(hours=6),
            arrival, release, Tariff(0, D('10.00')), GOLD, 1, at,
            zone='Asia/Shanghai')
        self.assertIs(again, receipt)
        self.assertTrue(service.workflow().stream.verify_chain())
        self.assertTrue(service.workflow().trail.verify())
        self.assertTrue(service.workflow().compensation.verify())

    def test_revision_through_workflow_nets_to_revised_basis(self):
        service = RailForgeService()
        arrival = datetime(2026, 3, 1, 16, tzinfo=U)
        release = datetime(2026, 3, 2, 0, tzinfo=U)
        tariff = Tariff(0, D('10.00'))
        service.features.auto_compensate('op-1', 'CLM-2', 'ACME', [obs(60, seq=1)],
                                         timedelta(hours=6), arrival, release,
                                         tariff, GOLD, 1,
                                         datetime(2026, 3, 2, 2, tzinfo=U),
                                         zone='Asia/Shanghai')
        service.features.auto_compensate('op-2', 'CLM-2', 'ACME', [obs(10, seq=2)],
                                         timedelta(hours=6), arrival, release,
                                         tariff, GOLD, 2,
                                         datetime(2026, 3, 2, 4, tzinfo=U),
                                         zone='Asia/Shanghai')
        engine = service.workflow().compensation
        live = live_entries(list(engine.state().entries))
        self.assertEqual(balance(live), D('80.00'))
        self.assertNotIn('service_credit', by_category(live))
        self.assertTrue(engine.verify())


if __name__ == '__main__':
    unittest.main()
