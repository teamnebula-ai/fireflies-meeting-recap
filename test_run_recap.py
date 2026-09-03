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

import json
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


class TestComposioIdentity(unittest.TestCase):
    """The send path must name the mailbox, and must not call a failed tool a success.

    Live incident, 2026-08-21..09-03: the box carried a Composio key from an org
    that had been retired, so every GMAIL_SEND_EMAIL and every fallback draft
    answered 401 for two weeks. Nothing in the spawn log said so, and a Composio
    reply of HTTP 200 with `successful: false` was being counted as a delivered
    email. These pin the wire shape and the failure classification.
    """

    def setUp(self):
        self._orig_req = run_recap._req
        self._orig_env = {k: os.environ.get(k) for k in
                          ("COMPOSIO_API_KEY", "COMPOSIO_USER_ID", "COMPOSIO_CONNECTED_ACCOUNT_ID")}
        os.environ["COMPOSIO_API_KEY"] = "ak_test"
        os.environ["COMPOSIO_USER_ID"] = "tmn-shawn"
        os.environ.pop("COMPOSIO_CONNECTED_ACCOUNT_ID", None)
        self.calls = []

    def tearDown(self):
        run_recap._req = self._orig_req
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _fake_req(self, reply):
        def fake(method, url, headers=None, body=None, timeout=60):
            self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
            return reply
        run_recap._req = fake

    def test_connected_account_id_is_sent_when_configured(self):
        os.environ["COMPOSIO_CONNECTED_ACCOUNT_ID"] = "ca_abc123"
        self._fake_req({"successful": True, "data": {}})
        run_recap.create_draft("me@example.com", "s", "<html></html>")
        call = self.calls[0]
        self.assertTrue(call["url"].endswith("/GMAIL_CREATE_EMAIL_DRAFT"))
        self.assertEqual(call["headers"]["x-api-key"], "ak_test")
        self.assertEqual(call["body"]["user_id"], "tmn-shawn")
        self.assertEqual(call["body"]["connected_account_id"], "ca_abc123")

    def test_connected_account_id_is_omitted_when_not_configured(self):
        self._fake_req({"successful": True, "data": {}})
        run_recap.send_email(["a@example.com", "b@example.com"], "s", "<html></html>")
        body = self.calls[0]["body"]
        self.assertNotIn("connected_account_id", body)
        self.assertEqual(body["arguments"]["recipient_email"], "a@example.com")
        self.assertEqual(body["arguments"]["extra_recipients"], ["b@example.com"])

    def test_http_200_with_successful_false_is_a_failure(self):
        self._fake_req({"successful": False, "error": "Precondition check failed", "data": None})
        r = run_recap.send_email(["a@example.com"], "s", "<html></html>")
        self.assertTrue(isinstance(r, dict) and r.get("_error"), r)
        self.assertIn("Precondition check failed", r["_error"])

    def test_http_error_stays_a_failure(self):
        self._fake_req({"_error": "HTTP 401: Invalid API key"})
        r = run_recap.create_draft("me@example.com", "s", "<html></html>")
        self.assertIn("HTTP 401", r["_error"])

    def test_missing_credentials_never_reach_the_wire(self):
        os.environ.pop("COMPOSIO_USER_ID")
        self._fake_req({"successful": True})
        r = run_recap.send_email(["a@example.com"], "s", "<html></html>")
        self.assertIn("missing", r["_error"])
        self.assertEqual(self.calls, [])


class TestFetchDiagnosis(unittest.TestCase):
    """A transcript the key cannot see must say so in words that point at the cause.

    Live incident, 2026-09-03: the Fireflies webhook started delivering IDs from
    the teamnebula.ai workspace while the box's key belonged to a different
    workspace. Every recap went "ambiguous" with a 200-character JSON dump as
    the reason and nobody could tell a deleted transcript from a wrong key.
    """

    def setUp(self):
        self._orig_req = run_recap._req
        self._orig_key = os.environ.get("FIREFLIES_API_KEY")
        os.environ["FIREFLIES_API_KEY"] = "ff_test"

    def tearDown(self):
        run_recap._req = self._orig_req
        if self._orig_key is None:
            os.environ.pop("FIREFLIES_API_KEY", None)
        else:
            os.environ["FIREFLIES_API_KEY"] = self._orig_key

    def test_object_not_found_names_the_workspace_mismatch(self):
        run_recap._req = lambda *a, **k: {
            "errors": [{"friendly": True, "message": "Transcript not found", "code": "object_not_found",
                        "extensions": {"code": "object_not_found"}}],
            "data": {"transcript": None}}
        t, err = run_recap.fetch_transcript("01ABC")
        self.assertIsNone(t)
        self.assertIn("Transcript not found", err)
        self.assertIn("another Fireflies workspace", err)

    def test_other_null_answers_keep_the_plain_reason(self):
        run_recap._req = lambda *a, **k: {"data": {"transcript": None}}
        t, err = run_recap.fetch_transcript("01ABC")
        self.assertIsNone(t)
        self.assertTrue(err.startswith("transcript null"))
        self.assertNotIn("another Fireflies workspace", err)


class TestGenerationTransport(unittest.TestCase):
    """RECAP_GEN_STDIN=1 moves the prompt off argv.

    Linux caps a single argv element at 128 KiB. A two-hour transcript is about
    100 KB before the writing spec and the Linear backlog are added, and the live
    box hit `OSError: [Errno 7] Argument list too long` on exactly that path.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.record = Path(self.tmp.name) / "calls.jsonl"
        fake = Path(self.tmp.name) / "fake-gen"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "args = sys.argv[1:]\n"
            "data = sys.stdin.read() if '-' in args else ''\n"
            f"open({str(self.record)!r}, 'a').write(json.dumps({{'argv': args, 'stdin_len': len(data)}}) + '\\n')\n"
            "print('<html><body>' + ('<p>recap line</p>' * 40) + '</body></html>')\n"
        )
        fake.chmod(0o755)
        self._saved = (run_recap.GEN_BIN, run_recap.GEN_MODEL, run_recap.GEN_STDIN, run_recap.GEN_TIMEOUT)
        run_recap.GEN_BIN = str(fake)
        run_recap.GEN_MODEL = "test/model"
        run_recap.GEN_TIMEOUT = 30
        self.transcript = {"title": "Big", "sentences": [{"speaker_name": "A", "text": "word " * 60}] * 300}

    def tearDown(self):
        run_recap.GEN_BIN, run_recap.GEN_MODEL, run_recap.GEN_STDIN, run_recap.GEN_TIMEOUT = self._saved
        self.tmp.cleanup()

    def _calls(self):
        return [json.loads(l) for l in self.record.read_text().splitlines() if l.strip()]

    def test_argv_mode_is_the_default_and_unchanged(self):
        run_recap.GEN_STDIN = False
        html = run_recap.generate_html("internal", self.transcript)
        self.assertIn("<html", html)
        call = self._calls()[0]
        self.assertEqual(call["argv"][:3], ["-m", "test/model", "-z"])
        self.assertTrue(call["argv"][3].startswith(run_recap.writing_spec()[:20]))
        self.assertEqual(call["stdin_len"], 0)

    def test_stdin_mode_sends_dash_and_the_whole_prompt_on_stdin(self):
        run_recap.GEN_STDIN = True
        html = run_recap.generate_html("internal", self.transcript)
        self.assertIn("<html", html)
        call = self._calls()[0]
        self.assertEqual(call["argv"], ["-m", "test/model", "-z", "-"])
        self.assertGreater(call["stdin_len"], 50_000)

    def test_structured_generation_uses_the_same_transport(self):
        run_recap.GEN_STDIN = True
        fake = Path(run_recap.GEN_BIN)
        fake.write_text(fake.read_text().replace("print('<html>", "print('{\"ok\": true}') or print('<html>"))
        out = run_recap.generate_json("please answer with json " * 5000)
        self.assertEqual(out, {"ok": True})
        self.assertEqual(self._calls()[0]["argv"], ["-m", "test/model", "-z", "-"])
        self.assertGreater(self._calls()[0]["stdin_len"], 100_000)

    def test_load_env_reads_the_flag(self):
        saved = os.environ.get("RECAP_GEN_STDIN")
        try:
            os.environ["RECAP_GEN_STDIN"] = "1"
            run_recap.load_env()
            self.assertTrue(run_recap.GEN_STDIN)
            os.environ["RECAP_GEN_STDIN"] = "0"
            run_recap.load_env()
            self.assertFalse(run_recap.GEN_STDIN)
        finally:
            if saved is None:
                os.environ.pop("RECAP_GEN_STDIN", None)
            else:
                os.environ["RECAP_GEN_STDIN"] = saved
            run_recap.load_env()


class TestLogTimestamps(unittest.TestCase):
    """Every spawn-log line carries a UTC timestamp.

    The live log had none, so a run that produced no output could not be placed
    in time at all and the failure window had to be reconstructed from lock-file
    mtimes.
    """

    def test_log_line_starts_with_utc_timestamp(self):
        import io
        buf = io.StringIO()
        saved = run_recap.sys.stderr
        run_recap.sys.stderr = buf
        try:
            run_recap.log("hello")
        finally:
            run_recap.sys.stderr = saved
        line = buf.getvalue()
        self.assertRegex(line, r"^\[recap \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\] hello\n$")
