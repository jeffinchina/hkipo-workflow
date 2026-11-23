from datetime import date, datetime
from pathlib import Path
import tempfile
import unittest
from hkipo.reminders import HK, HKBusinessCalendar, CalendarUnavailable, ReminderStore


def at(s): return datetime.fromisoformat(s).replace(tzinfo=HK)


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'reminders.db'
        self.store = ReminderStore(self.path)
    def tearDown(self): self.tmp.cleanup()
    def schedule(self, **kw):
        args=dict(source_hash='abc',evidence={'url':'official timetable'},now=at('2026-11-19T12:00'))
        args.update(kw)
        return self.store.schedule('09941:2026-11', '2026-11-19', **args)
    def test_calendar(self):
        c=HKBusinessCalendar()
        for close, expected in [('2026-11-19','2026-11-20'),('2026-11-20','2026-11-23'),('2026-04-02','2026-04-08'),('2026-12-24','2026-12-28'),('2026-12-31','2027-01-04'),('2027-02-05','2027-02-10'),('2027-12-24','2027-12-28')]:
            self.assertEqual(c.next_business_day(date.fromisoformat(close)).isoformat(),expected)
        with self.assertRaises(CalendarUnavailable): c.next_business_day(date(2027,12,31))
    def test_slots_restart_and_receipts(self):
        rows=self.schedule()
        self.assertEqual(len({r['event_id'] for r in rows}),1)
        a=self.store.due_reservations(at('2026-11-20T16:30'))
        self.assertEqual([r['slot'] for r in a],['16:30'])
        self.store=ReminderStore(self.path)
        self.assertEqual(self.store.due_reservations(at('2026-11-20T16:40')),[])
        self.store.finish_confirmed(a[0]['issue_id'],'16:30','receipt')
        self.assertEqual([r['slot'] for r in self.store.due_reservations(at('2026-11-20T17:30'))],['17:30'])
    def test_source_change_same_schedule(self):
        self.schedule()
        self.schedule(source_hash='minor edit',now=at('2026-11-20T16:31'))
        self.assertEqual(len(self.store.due_reservations(at('2026-11-20T16:31'))),1)
    def test_first_launch_never_backfills(self):
        rows=self.schedule(now=at('2026-11-21T12:00'))
        self.assertEqual({r['status'] for r in rows},{'skipped_history'})
        self.assertEqual(self.store.due_reservations(at('2026-11-22T12:00')),[])
    def test_delay_cancel_and_reschedule(self):
        self.schedule()
        self.assertEqual({r['status'] for r in self.schedule(status='delayed')},{'cancelled'})
        rows=self.schedule(override_date='2026-11-23',override_evidence={'url':'amendment'})
        self.assertTrue(all(r['due_at'].startswith('2026-11-23') for r in rows))
        self.assertEqual({r['status'] for r in self.schedule(status='withdrawn')},{'cancelled'})
    def test_unknown_or_sent_cannot_silently_repeat(self):
        self.schedule()
        self.store.due_reservations(at('2026-11-20T16:30'))
        rows=self.schedule(override_date='2026-11-23',override_evidence='amendment')
        self.assertEqual(rows[0]['status'],'unknown')
        self.assertEqual(rows[0]['correction_required'],1)
        self.assertEqual(len(self.store.due_reservations(at('2026-11-23T18:00'))),1)
    def test_evidence_and_timezone_required(self):
        with self.assertRaises(ValueError): self.schedule(now=datetime(2026,11,19))
        with self.assertRaises(ValueError): self.schedule(override_date='2026-11-23')
