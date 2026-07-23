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


class TestBlockedDomains(unittest.TestCase):
    """RECAP_BLOCKED_DOMAINS: no automated recap may be ADDRESSED to a blocked
    inbox, while classification still sees the true attendee list."""

    def setUp(self):
        self._orig = run_recap.BLOCKED_DOMAINS
        run_recap.BLOCKED_DOMAINS = {"lockeddown.com"}

    def tearDown(self):
        run_recap.BLOCKED_DOMAINS = self._orig

    def test_classification_still_sees_blocked_guest(self):
        # internal + blocked guest is still a sales call -> team gets the debrief
        mode, fmt, recips, _ = run_recap.classify(
            _t(["host@example.com", "guest@lockeddown.com"]))
        self.assertEqual(mode, "sales")
        self.assertEqual(recips, ["host@example.com"])

    def test_blocked_stripped_from_every_route(self):
        internal = ["host@example.com"]
        allm = ["host@example.com", "guest@lockeddown.com", "lee@acme.com"]
        sends = run_recap.route_sends("sales", internal, allm)
        for s in sends:
            self.assertNotIn("guest@lockeddown.com", s["recipients"],
                             f"{s['kind']} addressed a blocked inbox")
        # genuine external guest still gets the client recap
        kinds = {s["kind"]: s["recipients"] for s in sends}
        self.assertIn("lee@acme.com", kinds["client"])

    def test_no_client_recap_when_only_guest_is_blocked(self):
        internal = ["host@example.com"]
        allm = ["host@example.com", "guest@lockeddown.com"]
        sends = run_recap.route_sends("sales", internal, allm)
        kinds = {s["kind"] for s in sends}
        self.assertIn("sales_debrief", kinds)   # team still gets the debrief
        self.assertNotIn("client", kinds)       # nobody left to client-recap

    def test_internal_route_filters_blocked(self):
        allm = ["a@example.com", "b@lockeddown.com"]
        sends = run_recap.route_sends("internal", allm, allm)
        self.assertEqual(sends[0]["recipients"], ["a@example.com"])

    def test_all_blocked_routes_nothing(self):
        allm = ["a@lockeddown.com"]
        self.assertEqual(run_recap.route_sends("internal", allm, allm), [])

    def test_is_blocked_recipient_case_insensitive(self):
        self.assertTrue(run_recap.is_blocked_recipient("X@LockedDown.com".lower()))
        self.assertFalse(run_recap.is_blocked_recipient("x@example.com"))


class TestEmailAliases(unittest.TestCase):
    """RECAP_EMAIL_ALIASES: teammates on a second address classify as internal."""

    def setUp(self):
        self._orig = run_recap.EMAIL_ALIASES
        run_recap.EMAIL_ALIASES = {"jane@agency.co": "jane@example.com"}

    def tearDown(self):
        run_recap.EMAIL_ALIASES = self._orig

    def test_aliased_teammate_classifies_internal(self):
        mode, fmt, recips, _ = run_recap.classify(
            _t(["host@example.com", "jane@agency.co"]))
        self.assertEqual(mode, "internal")
        self.assertEqual(set(recips), {"host@example.com", "jane@example.com"})

    def test_alias_dedupes_against_canonical(self):
        emails = run_recap.attendee_emails(
            _t(["jane@example.com", "jane@agency.co"]))
        self.assertEqual(emails, ["jane@example.com"])

    def test_unaliased_domain_peer_stays_external(self):
        # only jane is aliased; a colleague at the same agency domain is a guest
        mode, fmt, recips, _ = run_recap.classify(
            _t(["host@example.com", "sam@agency.co"]))
        self.assertEqual(mode, "sales")
        self.assertEqual(recips, ["host@example.com"])


class TestClientSafetyScan(unittest.TestCase):
    def test_clean_client_html_passes(self):
        self.assertIsNone(run_recap.client_safety_violation(
            "<html><body><p>Thanks for your time today.</p></body></html>"))

    def test_prohibited_phrases_flagged(self):
        for phrase in ("internal debrief", "Deal Health", "competitive intel",
                       "budget authority", "Fireflies transcript", "recording link"):
            html = f"<html><body><p>see the {phrase} for details</p></body></html>"
            self.assertEqual(run_recap.client_safety_violation(html).lower(),
                             phrase.lower(), phrase)

    def test_empty_html_passes(self):
        self.assertIsNone(run_recap.client_safety_violation(""))
        self.assertIsNone(run_recap.client_safety_violation(None))


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

    def test_claim_and_ledger_state_are_owner_only(self):
        mid = "01KW9T2YQJBMEB3S2WS3DZSKJC"
        self.assertTrue(run_recap.claim(mid))
        run_recap.ledger_add(mid)

        claim_dir = run_recap.LEDGER.parent / "recap-claims"
        claim_file = claim_dir / f"{mid}.lock"
        self.assertEqual(claim_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(claim_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(run_recap.LEDGER.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(run_recap.LEDGER.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
