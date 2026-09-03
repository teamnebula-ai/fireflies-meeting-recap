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
from unittest import mock

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


class TestOwnerAttendance(unittest.TestCase):
    def setUp(self):
        self._orig_owner = run_recap.OWNER_EMAIL
        self._orig_names = os.environ.get("RECAP_OWNER_NAMES")
        run_recap.OWNER_EMAIL = "shawn@example.com"
        os.environ["RECAP_OWNER_NAMES"] = "Shawn Reddy,Shawn"

    def tearDown(self):
        run_recap.OWNER_EMAIL = self._orig_owner
        if self._orig_names is None:
            os.environ.pop("RECAP_OWNER_NAMES", None)
        else:
            os.environ["RECAP_OWNER_NAMES"] = self._orig_names

    def test_owner_joined_when_attendance_has_owner_name(self):
        transcript = {"meeting_attendance": [{"name": "Shawn Reddy"}]}
        self.assertTrue(run_recap.owner_joined_meeting(transcript))

    def test_owner_did_not_join_when_only_invited_attendee(self):
        transcript = {
            "meeting_attendees": [{"email": "shawn@example.com", "displayName": "Shawn Reddy"}],
            "meeting_attendance": [{"name": "Ibrahim Zia"}],
        }
        self.assertFalse(run_recap.owner_joined_meeting(transcript))

    def test_owner_name_matching_uses_word_boundaries(self):
        self.assertFalse(run_recap.owner_joined_meeting({"meeting_attendance": [{"name": "Shawna Fields"}]}))
        self.assertFalse(run_recap.owner_joined_meeting({"meeting_attendance": [{"name": "Rashawn Patel"}]}))
        self.assertTrue(run_recap.owner_joined_meeting({"meeting_attendance": [{"name": "Shawn Reddy"}]}))

    def test_missing_attendance_does_not_block_legacy_transcripts(self):
        transcript = {"meeting_attendees": [{"email": "shawn@example.com"}]}
        self.assertTrue(run_recap.owner_joined_meeting(transcript))


class TestSignature(unittest.TestCase):
    def setUp(self):
        self._orig_display = run_recap.OWNER_DISPLAY_NAME
        run_recap.OWNER_DISPLAY_NAME = "Shawn"

    def tearDown(self):
        run_recap.OWNER_DISPLAY_NAME = self._orig_display

    def test_replaces_other_attendee_signature(self):
        html = "<html><body><p>Notes.</p><p>Best,<br>Ibrahim</p></body></html>"
        out = run_recap.enforce_owner_signature(html)
        self.assertIn("<p>Best,<br>Shawn</p>", out)
        self.assertNotIn("Ibrahim", out)

    def test_preserves_earlier_thanks_paragraph(self):
        html = (
            "<html><body><p>Thanks for meeting today. We covered launch timing.</p>"
            "<p>Next step: send the launch plan.</p><p>Best,<br>Ibrahim</p></body></html>"
        )
        out = run_recap.enforce_owner_signature(html)
        self.assertIn("Thanks for meeting today", out)
        self.assertIn("Next step: send the launch plan", out)
        self.assertIn("<p>Best,<br>Shawn</p>", out)
        self.assertNotIn("Ibrahim", out)

    def test_inserts_signature_when_missing(self):
        html = "<html><body><p>Notes.</p></body></html>"
        out = run_recap.enforce_owner_signature(html)
        self.assertIn("<p>Best,<br>Shawn</p>\n</body>", out)

    def test_removes_earlier_signer_before_appending_shawn(self):
        html = (
            "<html><body><p>Notes.</p><p>Best,<br>Ibrahim</p>"
            "<p>Reply if I missed anything.</p></body></html>"
        )
        out = run_recap.enforce_owner_signature(html)
        self.assertNotIn("Ibrahim", out)
        self.assertEqual(out.count("Best,<br>Shawn"), 1)
        self.assertIn("Reply if I missed anything", out)

    def test_preserves_content_paragraph_beginning_with_thanks(self):
        html = (
            "<html><body><p>Notes.</p>"
            "<p>Thanks for walking us through the inventory workflow.</p>"
            "</body></html>"
        )
        out = run_recap.enforce_owner_signature(html)
        self.assertIn("Thanks for walking us through the inventory workflow", out)
        self.assertIn("<p>Best,<br>Shawn</p>", out)


class TestHtmlFormatting(unittest.TestCase):
    def setUp(self):
        self._orig_display = run_recap.OWNER_DISPLAY_NAME
        run_recap.OWNER_DISPLAY_NAME = "Shawn"

    def tearDown(self):
        run_recap.OWNER_DISPLAY_NAME = self._orig_display

    def test_cleans_fences_and_model_chatter_without_touching_inline_styles(self):
        raw = (
            "Here is the recap:\n```html\n"
            '<html><body style="font-family: Arial; color: #333;">'
            "<p>Good talking with you today.</p></body></html>\n```\nHope this helps."
        )
        out = run_recap._clean_html(raw)
        self.assertTrue(out.startswith("<html>"))
        self.assertTrue(out.endswith("</html>"))
        self.assertIn('style="font-family: Arial; color: #333;"', out)
        self.assertNotIn("```", out)
        self.assertNotIn("Here is the recap", out)

    def test_repairs_missing_closing_body_and_html_tags(self):
        out = run_recap._clean_html(
            "<html><body><p>Good talking with you today.</p>"
        )
        self.assertEqual(
            out,
            "<html><body><p>Good talking with you today.</p>\n</body>\n</html>",
        )

    def test_complete_signed_email_passes_validation(self):
        html = (
            '<html><body style="font-family: Arial, sans-serif;">'
            "<p>Hi Maya,</p><p>I enjoyed our conversation about the pilot.</p>"
            "<ul><li>We agreed to review the scope Friday.</li></ul>"
            "<p>Best,<br>Shawn</p></body></html>"
        )
        self.assertIsNone(run_recap.html_format_violation(html))

    def test_rejects_placeholders_empty_collections_and_markdown(self):
        samples = [
            ("placeholder", "<html><body><p>Hi [client name],</p><p>Best,<br>Shawn</p></body></html>"),
            ("empty list", "<html><body><ul>  </ul><p>Best,<br>Shawn</p></body></html>"),
            ("empty table", "<html><body><table></table><p>Best,<br>Shawn</p></body></html>"),
            ("empty list", "<html><body><ul><li> </li></ul><p>Best,<br>Shawn</p></body></html>"),
            ("empty table", (
                "<html><body><table><tr><td>&nbsp;</td></tr></table>"
                "<p>Best,<br>Shawn</p></body></html>"
            )),
            ("markdown fence", "```html\n<html><body><p>Hi.</p><p>Best,<br>Shawn</p></body></html>\n```"),
        ]
        for expected, html in samples:
            with self.subTest(expected=expected):
                self.assertIn(expected, run_recap.html_format_violation(html))

    def test_rejects_unbalanced_or_email_incompatible_markup(self):
        samples = {
            "nesting": (
                "<html><body><p><strong>Hello.</p></strong>"
                "<p>Best,<br>Shawn</p></body></html>"
            ),
            "unsupported <script>": (
                "<html><body><script>alert(1)</script>"
                "<p>Best,<br>Shawn</p></body></html>"
            ),
            "unsupported class attribute": (
                '<html><body><p class="intro">Hello.</p>'
                "<p>Best,<br>Shawn</p></body></html>"
            ),
            "onclick": (
                '<html><body><p onclick="alert(1)">Hello.</p>'
                "<p>Best,<br>Shawn</p></body></html>"
            ),
            "unsafe link": (
                '<html><body><p><a href="javascript:alert(1)">Open</a></p>'
                "<p>Best,<br>Shawn</p></body></html>"
            ),
            "onclick": (
                '<html><body><p>Hello.<br onclick="alert(1)"/></p>'
                "<p>Best,<br>Shawn</p></body></html>"
            ),
            "unsupported style attribute": (
                '<html><body><p>Hello.<br style="background:url(https://bad.test/x)"/></p>'
                "<p>Best,<br>Shawn</p></body></html>"
            ),
            "placement": (
                "<html><body><p><table><tr><td>Bad layout</td></tr></table></p>"
                "<p>Best,<br>Shawn</p></body></html>"
            ),
        }
        for expected, html in samples.items():
            with self.subTest(expected=expected):
                self.assertIn(expected, run_recap.html_format_violation(html))

    def test_rejects_incomplete_structure_and_wrong_signature(self):
        self.assertIn(
            "structure",
            run_recap.html_format_violation(
                "<html><body><p>Hello.</p><p>Best,<br>Shawn</p></body>"
            ),
        )
        self.assertIn(
            "signature",
            run_recap.html_format_violation(
                "<html><body><p>Hello.</p><p>Best,<br>Ibrahim</p></body></html>"
            ),
        )

    def test_generation_retries_after_format_violation(self):
        invalid = "<html><body><p>Hi [name],</p></body></html>"
        valid = (
            '<html><body style="font-family: Arial, sans-serif; color: #333; '
            'line-height: 1.6; max-width: 760px; margin: 0 auto;">'
            "<p>Hi Maya,</p><p>Your pilot timeline gave us a clear place to start.</p>"
            "<h2 style=\"color: #1a73e8;\">Next steps</h2>"
            "<ul><li>We'll send the scope Friday, then you can mark up any gaps.</li></ul>"
            "<p>Best,<br>Shawn</p></body></html>"
        )
        responses = [
            mock.Mock(returncode=0, stdout=invalid, stderr=""),
            mock.Mock(returncode=0, stdout=valid, stderr=""),
        ]
        with mock.patch.object(run_recap, "_which", return_value=True), mock.patch.object(
                run_recap.subprocess, "run", side_effect=responses) as run:
            out = run_recap.generate_html("client", {"title": "Pilot planning"})
        self.assertIn("Your pilot timeline gave us a clear place to start", out)
        self.assertIn('style="font-family: Arial, sans-serif;', out)
        self.assertEqual(out.count("Best,<br>Shawn"), 1)
        self.assertEqual(run.call_count, 2)

    def test_wrong_model_signer_is_repaired_without_another_generation(self):
        wrong_signer = (
            '<html><body style="font-family: Arial, sans-serif; color: #333; '
            'line-height: 1.6; max-width: 760px; margin: 0 auto;">'
            "<p>Hi Maya,</p><p>Your pilot timeline gave us a clear place to start.</p>"
            "<h2>Next steps</h2><ul><li>We'll send the pilot scope Friday.</li></ul>"
            "<p>Best,<br>Ibrahim</p></body></html>"
        )
        response = mock.Mock(returncode=0, stdout=wrong_signer, stderr="")
        with mock.patch.object(run_recap, "_which", return_value=True), mock.patch.object(
                run_recap.subprocess, "run", return_value=response) as run:
            out = run_recap.generate_html("client", {"title": "Pilot planning"})
        self.assertIn("<p>Best,<br>Shawn</p>", out)
        self.assertNotIn("Ibrahim", out)
        self.assertEqual(run.call_count, 1)


class TestRecapVoice(unittest.TestCase):
    def test_rejects_canned_email_copy(self):
        html = (
            "<html><body><p>It was great connecting about several important topics.</p>"
            "<p>Best,<br>Shawn</p></body></html>"
        )
        self.assertIn("canned phrase", run_recap.recap_voice_violation(
            html, {"title": "Rivus pilot"}))

    def test_rejects_copy_with_no_meeting_anchor(self):
        html = (
            "<html><body><p>We covered the plan and will follow up soon.</p>"
            "<p>Best,<br>Shawn</p></body></html>"
        )
        self.assertEqual(
            run_recap.recap_voice_violation(
                html, {"title": "Rivus security pilot", "summary": "Maya owns access"}),
            "no concrete meeting detail",
        )

    def test_metadata_keys_do_not_count_as_meeting_details(self):
        html = (
            "<html><body><p>Reply to this email if you have questions.</p>"
            "<p>Best,<br>Shawn</p></body></html>"
        )
        transcript = {
            "title": "ERP warehouse review",
            "meeting_attendees": [{"email": "maya@example.com"}],
            "sentences": [{"text": "Inventory reconciliation is due Friday"}],
        }
        self.assertEqual(
            run_recap.recap_voice_violation(html, transcript),
            "no concrete meeting detail",
        )

    def test_accepts_specific_detail_from_transcript(self):
        html = (
            "<html><body><p>Maya, we'll send the Rivus pilot scope Friday.</p>"
            "<p>Best,<br>Shawn</p></body></html>"
        )
        self.assertIsNone(run_recap.recap_voice_violation(
            html, {"title": "Rivus security pilot", "summary": "Maya owns access"}))


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

    def test_external_recipients_are_allowed_by_default(self):
        internal = ["host@example.com"]
        allm = ["host@example.com", "guest@acme.com"]
        sends = run_recap.route_sends("sales", internal, allm)
        kinds = {s["kind"]: s["recipients"] for s in sends}
        self.assertEqual(kinds["client"], allm)

    def test_mixed_external_meeting_addresses_all_unblocked_guests(self):
        internal = ["host@example.com"]
        allm = internal + ["guest@rivus.mx", "observer@acme.com"]
        sends = run_recap.route_sends("sales", internal, allm)
        kinds = {s["kind"]: s["recipients"] for s in sends}
        self.assertEqual(kinds["client"], allm)

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


class TestBlockedExternalContext(unittest.TestCase):
    def setUp(self):
        self._orig_domains = run_recap.BLOCKED_DOMAINS
        self._orig_terms = run_recap.BLOCKED_EXTERNAL_TERMS
        run_recap.BLOCKED_DOMAINS = {"rs21.io"}
        run_recap.BLOCKED_EXTERNAL_TERMS = {
            "rs21", "research innovations", "brady key", "nmosa", "unmccc"
        }

    def tearDown(self):
        run_recap.BLOCKED_DOMAINS = self._orig_domains
        run_recap.BLOCKED_EXTERNAL_TERMS = self._orig_terms

    def test_rs21_email_blocks_every_client_facing_send(self):
        transcript = _t(["host@example.com", "brady@rs21.io", "guest@acme.com"])
        sends = run_recap.apply_external_context_gate(
            run_recap.route_sends(
                "sales", ["host@example.com"], run_recap.attendee_emails(transcript)),
            transcript,
        )
        self.assertEqual(
            sends,
            [{"kind": "sales_debrief", "recipients": ["host@example.com"]}],
        )

    def test_known_person_in_attendee_name_blocks_client_send(self):
        transcript = _t(["host@example.com", "guest@acme.com"])
        transcript["meeting_attendees"][1]["displayName"] = "Brady Key"
        self.assertTrue(run_recap.meeting_has_blocked_external_context(transcript))

    def test_rs21_topic_in_title_summary_or_transcript_blocks(self):
        samples = [
            {"title": "NMOSA delivery review"},
            {"summary": {"overview": "Research Innovations partnership update"}},
            {"sentences": [{"speaker_name": "Shawn", "text": "Next steps for UNMCCC"}]},
        ]
        for transcript in samples:
            with self.subTest(transcript=transcript):
                self.assertTrue(run_recap.meeting_has_blocked_external_context(transcript))

    def test_term_matching_uses_boundaries(self):
        self.assertFalse(run_recap.meeting_has_blocked_external_context(
            {"title": "Opening remarks for product review"}))

    def test_unrelated_external_meeting_remains_sendable(self):
        transcript = _t(["host@example.com", "guest@acme.com"])
        transcript["title"] = "Prospect discovery"
        self.assertFalse(run_recap.meeting_has_blocked_external_context(transcript))


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
