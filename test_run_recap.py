#!/usr/bin/env python3
"""Unit tests for the deterministic, safety-critical parts of run_recap.py.

Run:  python3 -m unittest test_run_recap -v

Covers the two things that must never regress:
  1. Recipient routing — the internal debrief NEVER reaches an external address;
     the client recap goes to everyone.
  2. Atomic claim() — duplicate Fireflies events for one meeting can't both
     proceed (the bug that sent 2 emails).

The default internal domain is "example.com" (RECAP_INTERNAL_DOMAIN). Internal
addresses here use @example.com; external use @acme.com.
"""
import os
os.environ.setdefault("RECAP_INTERNAL_DOMAIN", "example.com")

import tempfile
import unittest
from pathlib import Path

import run_recap


def _t(emails):
    return {"title": "Demo Call", "dateString": "2026-06-30T17:00:00.000Z",
            "meeting_attendees": [{"email": e, "displayName": e} for e in emails]}


class TestClassify(unittest.TestCase):
    def test_internal_only(self):
        mode, fmt, recips, _ = run_recap.classify(_t(["a@example.com", "b@example.com"]))
        self.assertEqual(mode, "internal")
        self.assertEqual(set(recips), {"a@example.com", "b@example.com"})

    def test_mixed_is_sales_internal_recipients_only(self):
        mode, fmt, recips, _ = run_recap.classify(_t(["host@example.com", "guest@acme.com"]))
        self.assertEqual(mode, "sales")
        self.assertEqual(recips, ["host@example.com"])  # guest excluded

    def test_external_only_is_ambiguous(self):
        mode, *_ = run_recap.classify(_t(["guest@acme.com"]))
        self.assertEqual(mode, "ambiguous")

    def test_no_emails_is_ambiguous(self):
        mode, *_ = run_recap.classify(_t([]))
        self.assertEqual(mode, "ambiguous")

    def test_attendee_emails_dedupes_and_lowercases(self):
        self.assertEqual(
            run_recap.attendee_emails(_t(["A@Example.com", "a@example.com"])),
            ["a@example.com"])


class TestRouting(unittest.TestCase):
    def test_internal_one_send_to_all(self):
        allm = ["a@example.com", "b@example.com"]
        sends = run_recap.route_sends("internal", allm, allm)
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]["kind"], "internal")
        self.assertEqual(sends[0]["recipients"], allm)

    def test_sales_two_sends_debrief_internal_client_all(self):
        internal = ["host@example.com"]
        allm = ["host@example.com", "guest@acme.com"]
        sends = run_recap.route_sends("sales", internal, allm)
        kinds = {s["kind"]: s["recipients"] for s in sends}
        self.assertEqual(kinds["sales_debrief"], ["host@example.com"])
        self.assertEqual(set(kinds["client"]), {"host@example.com", "guest@acme.com"})

    def test_internal_debrief_never_contains_external(self):
        internal = ["host@example.com", "ops@example.com"]
        allm = internal + ["guest@acme.com", "lee@acme.com"]
        sends = run_recap.route_sends("sales", internal, allm)
        external = {"guest@acme.com", "lee@acme.com"}
        for s in sends:
            if s["kind"] in ("internal", "sales_debrief"):
                self.assertFalse(external & set(s["recipients"]),
                                 f"{s['kind']} leaked an external address")

    def test_ambiguous_routes_nothing(self):
        self.assertEqual(run_recap.route_sends("ambiguous", [], ["guest@acme.com"]), [])


class TestAtomicClaim(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = run_recap.LEDGER
        run_recap.LEDGER = Path(self._tmp.name) / "state" / "recap-seen.json"

    def tearDown(self):
        run_recap.LEDGER = self._orig
        self._tmp.cleanup()

    def test_first_claim_wins_second_loses(self):
        mid = "01KW9T2YQJBMEB3S2WS3DZSKJC"
        self.assertTrue(run_recap.claim(mid))   # first event proceeds
        self.assertFalse(run_recap.claim(mid))  # duplicate event bails -> no 2nd email

    def test_distinct_meetings_each_claimable(self):
        self.assertTrue(run_recap.claim("meeting-A"))
        self.assertTrue(run_recap.claim("meeting-B"))

    def test_slash_in_id_is_safe(self):
        self.assertTrue(run_recap.claim("weird/id"))
        self.assertFalse(run_recap.claim("weird/id"))


if __name__ == "__main__":
    unittest.main()
