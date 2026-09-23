#!/usr/bin/env python3
"""run_recap.py — post-meeting recap driver.

Triggered by the Fireflies webhook (via receiver.py). Deterministic Python does
everything except the writing:

  1. claim         ← atomic O_EXCL lock so duplicate webhooks can't double-send
  2. dedupe        ← state/recap-seen.json (permanent, across restarts)
  3. fetch         ← Fireflies GraphQL (FIREFLIES_API_KEY)
  4. classify      ← recipients/send-mode purely by email domain (RECAP_INTERNAL_DOMAIN)
  5. generate      ← one-shot LLM CLI; stdout = recap HTML body ONLY
  6. send/draft    ← Composio Gmail v3 REST (GMAIL_SEND_EMAIL / *_CREATE_EMAIL_DRAFT)
  7. reconcile     ← validated Linear completion/create actions
  8. status        ← optional notify (Telegram via the gen CLI, if configured)

The LLM never decides recipients, never sends, and never directly mutates
Linear. It writes recap prose and proposes structured work; Python validates all
candidate IDs, transcript evidence, workspace relationships, and writes.

Recipient policy:
  - Internal meeting (all attendees @RECAP_INTERNAL_DOMAIN) → one recap to all attendees.
  - External/sales (>=1 outside attendee) → TWO artifacts:
      * internal debrief  → internal attendees ONLY (never the outside guest)
      * client recap      → Gmail draft addressed to every OTHER participant
                            (the owner sends it), in the same layout and
                            subject as the internal recap with client-safe
                            content rules; never auto-sent
  - If the recap owner did not personally join the meeting (based on Fireflies
    meeting_attendance), skip all recap sends/drafts.
  - Ambiguous (no emails, or outside guests but zero internal recipient, or fetch
    failed) → draft to RECAP_OWNER_EMAIL, no auto-send.

Configuration is entirely via environment variables (see recap.env.example).

Usage:
  run_recap.py --meeting-id <id> [--event "Transcription completed"] [--dry]
    --dry : fetch + classify + generate + print; send nothing; don't claim/mark seen.
"""
import argparse
import html as html_lib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

import linear_recap
from linear_api import LinearAPI

HERE = Path(__file__).resolve().parent
HOME = Path(os.path.expanduser("~"))

# ----- configuration (all via env; see recap.env.example) ---------------------
ENV_FILE = Path(os.environ.get("RECAP_ENV_FILE", str(HOME / "secrets" / "recap.env")))
STATE_DIR = Path(os.environ.get("RECAP_STATE_DIR", str(HOME / ".recap" / "state")))
LEDGER = STATE_DIR / "recap-seen.json"
WRITING_SPEC = Path(os.environ.get("RECAP_WRITING_SPEC", str(HERE / "templates" / "writing_spec.md")))

# Pluggable one-shot generation CLI: invoked as `<bin> -m <model> -z <prompt>` and
# expected to print the email HTML to stdout. Swap for any LLM CLI with that shape.
GEN_BIN = os.environ.get("RECAP_GEN_BIN", "hermes")
GEN_MODEL = os.environ.get("RECAP_GEN_MODEL", "gpt-5")
GEN_TIMEOUT = int(os.environ.get("RECAP_GEN_TIMEOUT", "600"))
# Prompt transport to GEN_BIN. Default: argv (`-z <prompt>`), which Linux caps
# at 128 KiB per argument — a two-hour transcript is ~100 KB before the writing
# spec and the Linear backlog are added, and the live box died on exactly that
# with `OSError: [Errno 7] Argument list too long`. RECAP_GEN_STDIN=1 sends
# `-z -` and the prompt on stdin instead; needs a GEN_BIN that reads `-` from
# stdin (contrib/hermes-remote does).
GEN_STDIN = os.environ.get("RECAP_GEN_STDIN", "0") == "1"

# Identity / routing
INTERNAL_DOMAIN = os.environ.get("RECAP_INTERNAL_DOMAIN", "example.com")
OWNER_EMAIL = os.environ.get("RECAP_OWNER_EMAIL", "")        # fallback/ambiguous draft target
OWNER_DISPLAY_NAME = os.environ.get("RECAP_OWNER_DISPLAY_NAME", "Shawn")
NOTIFY_TARGET = os.environ.get("RECAP_NOTIFY_TARGET", "")    # optional status pings (e.g. telegram:123)


def _parse_domains(raw):
    return {d.strip().lower().lstrip("@") for d in (raw or "").split(",") if d.strip()}


def _parse_aliases(raw):
    """'alias@a.com=canonical@b.com,...' -> {alias: canonical} (lowercased)."""
    out = {}
    for pair in (raw or "").split(","):
        if "=" not in pair:
            continue
        alias, canonical = pair.split("=", 1)
        alias, canonical = alias.strip().lower(), canonical.strip().lower()
        if "@" in alias and "@" in canonical:
            out[alias] = canonical
    return out


# Domains that must NEVER receive an automated recap (e.g. a client under a
# no-automation agreement). Filtered by DOMAIN, not by meeting tag, so the
# guarantee holds even when such a person joins a call about something else.
BLOCKED_DOMAINS = _parse_domains(os.environ.get("RECAP_BLOCKED_DOMAINS", ""))


def _parse_terms(raw):
    return {" ".join(t.strip().lower().split())
            for t in (raw or "").split(",") if t.strip()}


# People, project names, contract labels, and topics that suppress every
# client-facing recap while leaving the internal debrief enabled.
BLOCKED_EXTERNAL_TERMS = _parse_terms(
    os.environ.get("RECAP_BLOCKED_EXTERNAL_TERMS", ""))

# Domains whose presence on a call means NO client draft for that meeting at all
# (e.g. a partner whose client relationship we do not own, like RS21 and its
# project clients). Stronger than BLOCKED_DOMAINS, which only strips those
# addresses: stripping would still hand that meeting's recap to the other
# guests. Subdomains match. The internal debrief is unaffected.
NO_CLIENT_DRAFT_DOMAINS = _parse_domains(os.environ.get("RECAP_NO_CLIENT_DRAFT_DOMAINS", ""))

# Teammates who join meetings under a second address (e.g. an agency account)
# but are internal. Alias SPECIFIC people, not whole domains — aliasing a domain
# would classify a genuine guest at that domain as internal.
EMAIL_ALIASES = _parse_aliases(os.environ.get("RECAP_EMAIL_ALIASES", ""))

# Automated addresses that ride along on invites but are not people. Left in,
# a Zoom notification address makes a solo webinar look like an external meeting
# and would land on the client draft's To line.
NON_HUMAN_PREFIXES = ("no-reply@", "noreply@", "donotreply@", "do-not-reply@",
                      "calendar-notification@", "mailer-daemon@")

FIREFLIES_GQL = "https://api.fireflies.ai/graphql"
COMPOSIO_EXEC = "https://backend.composio.dev/api/v3/tools/execute"


def log(msg):
    # UTC timestamp on every line. The spawn log is the only record of a run
    # (the receiver detaches run_recap from journald), and a run that dies
    # between the claim and the first generation line used to leave nothing that
    # could be placed in time at all.
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sys.stderr.write(f"[recap {ts}] {msg}\n")
    sys.stderr.flush()


def load_env():
    """Source RECAP_ENV_FILE into os.environ (KEY=VALUE, ignore #/blank)."""
    global GEN_BIN, GEN_MODEL, GEN_TIMEOUT, GEN_STDIN, INTERNAL_DOMAIN, OWNER_EMAIL
    global OWNER_DISPLAY_NAME, NOTIFY_TARGET, WRITING_SPEC, BLOCKED_DOMAINS, EMAIL_ALIASES
    global NO_CLIENT_DRAFT_DOMAINS, BLOCKED_EXTERNAL_TERMS
    if not ENV_FILE.exists():
        log(f"note: env file {ENV_FILE} not present (relying on process env)")
    else:
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    # Module defaults are initialized before the env file is read. Refresh the
    # configurable values so recap.env works as documented.
    GEN_BIN = os.environ.get("RECAP_GEN_BIN", GEN_BIN)
    GEN_MODEL = os.environ.get("RECAP_GEN_MODEL", GEN_MODEL)
    GEN_TIMEOUT = int(os.environ.get("RECAP_GEN_TIMEOUT", str(GEN_TIMEOUT)))
    GEN_STDIN = os.environ.get("RECAP_GEN_STDIN", "1" if GEN_STDIN else "0") == "1"
    INTERNAL_DOMAIN = os.environ.get("RECAP_INTERNAL_DOMAIN", INTERNAL_DOMAIN)
    OWNER_EMAIL = os.environ.get("RECAP_OWNER_EMAIL", OWNER_EMAIL)
    OWNER_DISPLAY_NAME = os.environ.get("RECAP_OWNER_DISPLAY_NAME", OWNER_DISPLAY_NAME)
    NOTIFY_TARGET = os.environ.get("RECAP_NOTIFY_TARGET", NOTIFY_TARGET)
    WRITING_SPEC = Path(os.environ.get("RECAP_WRITING_SPEC", str(WRITING_SPEC)))
    BLOCKED_DOMAINS = _parse_domains(os.environ.get("RECAP_BLOCKED_DOMAINS", ""))
    NO_CLIENT_DRAFT_DOMAINS = _parse_domains(os.environ.get("RECAP_NO_CLIENT_DRAFT_DOMAINS", ""))
    BLOCKED_EXTERNAL_TERMS = _parse_terms(
        os.environ.get("RECAP_BLOCKED_EXTERNAL_TERMS", ""))
    EMAIL_ALIASES = _parse_aliases(os.environ.get("RECAP_EMAIL_ALIASES", ""))


# ----- idempotency ------------------------------------------------------------
def ledger_load():
    try:
        return json.loads(LEDGER.read_text())
    except Exception:
        return []


def ledger_add(mid):
    seen = ledger_load()
    if mid not in seen:
        seen.append(mid)
        LEDGER.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        LEDGER.parent.chmod(0o700)
        fd = os.open(str(LEDGER), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as ledger:
            ledger.write(json.dumps(seen))


def claim(mid):
    """Atomically claim a meeting-id so only ONE process ever proceeds.

    Fireflies fires multiple events for the same meeting (e.g.
    `meeting.transcribed` AND `meeting.summarized`); the receiver spawns a
    process per event. The ledger alone can't dedupe them — it's written only
    after the (slow) generate+send, so a second event's process passes the
    `mid in ledger_load()` check before the first has finished, and both send.

    O_CREAT|O_EXCL makes the claim atomic at the filesystem level: the first
    process to create the lock wins; every later one gets FileExistsError and
    bails. Returns True if WE claimed it, False if someone already had it.
    """
    d = LEDGER.parent / "recap-claims"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    d.chmod(0o700)
    p = d / (mid.replace("/", "_") + ".lock")
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as claim_file:
            claim_file.write(str(time.time()))
        return True
    except FileExistsError:
        return False


# ----- HTTP -------------------------------------------------------------------
def _req(method, url, headers=None, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw.decode()) if raw else {}
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}: {e.read().decode()[:600]}"}
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


# ----- Fireflies fetch --------------------------------------------------------
def fetch_transcript(meeting_id):
    key = os.environ.get("FIREFLIES_API_KEY")
    if not key:
        sys.exit("ERROR: FIREFLIES_API_KEY missing")
    q = ("query($id:String!){ transcript(id:$id){ id title dateString date "
         "duration transcript_url host_email organizer_email participants "
         "meeting_attendees { displayName email name } "
         "meeting_attendance { name join_time leave_time } "
         "summary { overview action_items keywords bullet_gist outline } "
         "sentences { speaker_name text } } }")
    r = _req("POST", FIREFLIES_GQL,
             {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
             {"query": q, "variables": {"id": meeting_id}}, timeout=90)
    if isinstance(r, dict) and r.get("_error"):
        return None, r["_error"]
    t = (r or {}).get("data", {}).get("transcript")
    if not t:
        # Fireflies answers `object_not_found` both for a deleted transcript and
        # for one that exists in a workspace this key cannot see. The second is
        # the one that bites: a webhook registered on another Fireflies account
        # delivers IDs the key will never resolve, and every recap goes
        # "ambiguous" with a JSON dump as the reason. Say which account holds
        # the key so the mismatch is the first thing read, not the last.
        errs = (r or {}).get("errors") if isinstance(r, dict) else None
        codes = set()
        for e in errs or []:
            if isinstance(e, dict):
                codes.add(str(e.get("code") or (e.get("extensions") or {}).get("code") or ""))
        hint = ""
        if "object_not_found" in codes:
            hint = (" — the FIREFLIES_API_KEY account cannot see this transcript; the webhook "
                    "that delivered it may belong to another Fireflies workspace (check the key "
                    "with `{ user { email } }`)")
        return None, f"transcript null (resp: {str(r)[:200]}){hint}"
    return t, None


# ----- classification (deterministic) -----------------------------------------
def attendee_emails(transcript):
    """De-duped, lowercased list of every attendee email on the transcript."""
    atts = transcript.get("meeting_attendees") or []
    emails = []
    for a in atts:
        e = canonical_email((a.get("email") or "").strip().lower())
        if e and "@" in e and e not in emails and not e.startswith(NON_HUMAN_PREFIXES):
            emails.append(e)
    return emails


def canonical_email(email):
    """Map an aliased teammate address to their canonical internal identity."""
    return EMAIL_ALIASES.get(email, email)


def is_owner(email):
    """True for the recap owner's own address (after alias folding)."""
    owner = canonical_email((OWNER_EMAIL or "").strip().lower())
    return bool(owner) and email == owner


def client_draft_exclusion(emails):
    """The first attendee domain that rules out a client draft, or None.

    Matches a listed domain exactly or as a parent domain (osa.nm.gov matches a
    listed nm.gov entry too, so list the narrowest domain you mean)."""
    for email in emails:
        domain = (email.split("@", 1)[1] if "@" in email else "").lower()
        for listed in NO_CLIENT_DRAFT_DOMAINS:
            if domain == listed or domain.endswith("." + listed):
                return domain
    return None


def is_blocked_recipient(email):
    domain = (email.split("@", 1)[1] if "@" in email else "").lower()
    return domain in BLOCKED_DOMAINS


def meeting_has_blocked_external_context(transcript):
    """True when a blocked domain, person, or topic appears in the meeting."""
    text = " ".join(json.dumps(transcript or {}, ensure_ascii=False).lower().split())
    for email_domain in re.findall(r"[a-z0-9._%+\-]+@([a-z0-9.\-]+)", text):
        if email_domain.rstrip(".") in BLOCKED_DOMAINS:
            return True
    for term in BLOCKED_EXTERNAL_TERMS:
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text):
            return True
    return False


def apply_external_context_gate(sends, transcript):
    """Suppress only the client-facing route for sensitive meetings."""
    if not meeting_has_blocked_external_context(transcript):
        return sends
    return [send for send in sends if send.get("kind") != "client"]


def _norm_name(value):
    return " ".join(str(value or "").strip().lower().split())


def owner_joined_meeting(transcript):
    """True unless Fireflies attendance proves the recap owner was absent.

    Fireflies separates invited calendar attendees (meeting_attendees) from the
    people who actually joined (meeting_attendance). Shawn should not receive or
    trigger recaps for meetings where Fireflies joined from his calendar but he
    did not personally attend.

    Older transcript payloads may not include meeting_attendance at all; keep
    those flowing rather than blocking on missing legacy data.
    """
    if "meeting_attendance" not in transcript:
        return True
    attendance = transcript.get("meeting_attendance") or []
    owner_names = [
        _norm_name(n)
        for n in os.environ.get("RECAP_OWNER_NAMES", "Shawn Reddy,Shawn").split(",")
        if _norm_name(n)
    ]
    if not owner_names:
        return True
    for attendee in attendance:
        name = _norm_name((attendee or {}).get("name"))
        tokens = set(re.findall(r"\b[\w'-]+\b", name))
        for owner in owner_names:
            if not owner:
                continue
            if " " in owner:
                if name == owner:
                    return True
            elif owner in tokens:
                return True
    return False


def classify(transcript):
    """Return (mode, fmt, recipients, ambiguous_reason).
    mode: 'internal' | 'sales' | 'ambiguous'. recipients: list of emails (To).
    For 'sales', recipients is the INTERNAL set only — the outside guest never
    receives the internal debrief."""
    emails = attendee_emails(transcript)
    if not emails:
        return "ambiguous", "internal", [], "no attendee emails on the transcript"
    internal = [e for e in emails if e.endswith("@" + INTERNAL_DOMAIN)]
    external = [e for e in emails if e not in internal]
    if external:
        if not internal:
            return ("ambiguous", "sales", [],
                    f"external attendees present ({', '.join(external)}) but no "
                    f"@{INTERNAL_DOMAIN} recipient to send the internal debrief to")
        return "sales", "sales", internal, None
    return "internal", "internal", emails, None


def route_sends(mode, internal_recipients, all_emails):
    """Pure recipient routing. Returns a list of delivery descriptors.

    kind:
      'internal'      -> internal-meeting recap, to all (internal) attendees
      'sales_debrief' -> internal debrief (summary/next-steps), INTERNAL ONLY
      'client'        -> client-facing draft, addressed to every participant
                         except the owner

    Safety invariant (enforced here, asserted by tests): a 'sales_debrief' send
    NEVER contains an external address. The only thing that reaches outside
    guests is the 'client' draft, which carries the client-safe HTML and stays
    in the owner's Gmail account until a person sends it. The owner sends that
    draft from their own mailbox, so they are its sender and never on its To line.

    Blocked domains (RECAP_BLOCKED_DOMAINS) are stripped from every route.
    Classification upstream still sees the true attendee list — a call with a
    blocked guest still counts as 'sales', so the team gets the candid debrief —
    but no recap is ever ADDRESSED to a blocked inbox.
    """
    sendable = lambda emails: [e for e in emails if not is_blocked_recipient(e)]
    if mode == "internal":
        recipients = sendable(all_emails)
        return [{"kind": "internal", "recipients": recipients}] if recipients else []
    if mode == "sales":
        sends = []
        debrief = sendable(internal_recipients)
        if debrief:
            sends.append({"kind": "sales_debrief", "recipients": debrief})
        client = [e for e in sendable(all_emails) if not is_owner(e)]
        # Only draft a client recap if a genuine external recipient remains after
        # filtering — a call whose only guest was blocked has no client to recap to —
        # and no attendee belongs to a no-client-draft domain (checked against the
        # TRUE attendee list, before any stripping).
        if (any(not e.endswith("@" + INTERNAL_DOMAIN) for e in client)
                and not client_draft_exclusion(all_emails)):
            sends.append({"kind": "client", "recipients": client})
        return sends
    return []  # ambiguous -> handled as a draft to the owner, not an auto-send


# ----- generation (LLM layer only) --------------------------------------------
def writing_spec():
    """The writing guidance and HTML templates (internal, sales, and client)."""
    try:
        return WRITING_SPEC.read_text()
    except Exception as e:
        sys.exit(f"ERROR: cannot read writing spec {WRITING_SPEC}: {e}")


# The client draft uses the INTERNAL recap's template from writing_spec.md, so
# guests get the same layout, styling, and standards the team gets from one
# source that cannot drift. This overlay changes only who the reader is: the
# greeting, the recording line, and what content may appear.
CLIENT_SPEC = """\
CLIENT-FACING RECAP. This email becomes a Gmail draft addressed to everyone else
who was on the call, INCLUDING the external guests. The host reviews and sends it.

FORMAT: use the "Email HTML Structure — Internal Meetings" template above
EXACTLY: the same inline styles, the same blue dividers, the 🧭 Meeting Overview
section, one numbered section per major topic with its Decision line, and the
✅ Action Items by Owner tables with priority tags. Apply every general standard
above except the recording link. Never use the External / Sales debrief
template. Change only these parts:
- Greeting: "Hi [first names of the other attendees, comma-separated]," in place of "Team,".
- Opening: 1-2 warm sentences thanking them for their time and stating the headline of the conversation.
- Action Items by Owner: one heading per owner on either side, by first name, covering only commitments made on the call.
- Leave out the Fireflies transcript/recording line entirely.
- Closing line: invite them to reply with questions or corrections.

CONTENT: the reader is the client. NEVER include any of the following:
- Internal intel, "deal health", deal stage, or how the call "felt".
- Budget speculation, pricing strategy, or guesses about their spend.
- Competitive intel or mentions of other vendors they're evaluating.
- Candid/internal assessments, "what didn't land", red flags, or buying signals.
- Internal-only tasks (CRM updates, "build a demo for their use case", research).
- The Fireflies transcript/recording link (that is internal only).
Write like Shawn is following up personally after the call:
- Open with a specific moment, decision, question, or shared goal from the meeting.
- Keep any thanks brief and connected to what the client contributed.
- Use a natural, conversational rhythm. "We" means Team Nebula and "you" means
  the client.
- Avoid canned openers, corporate filler, fake enthusiasm, em dashes, and repeated
  sentence patterns.
- Never invent familiarity or imply a relationship the transcript does not show.
- Omit any section, list, or row with no real content. Never emit placeholders.

HTML RULES:
- Return one complete document from <html> through </html>.
- Use inline styles only. Do not use CSS classes, <style>, flexbox, grid, scripts,
  images, Markdown, or emoji headings.
- Keep paragraphs short and preserve the simple, single-column layout below.
- End with exactly <p>Best,<br>Shawn</p>.

Use this HTML structure, omitting optional sections that have no supported content:

<html><body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6; max-width: 760px; margin: 0 auto;">
<p>Hi [first names of the client attendees, comma-separated],</p>
<p>[One or two natural sentences tied to a real detail from the conversation.]</p>

<h2 style="color: #1a73e8; font-size: 18px; margin: 24px 0 8px;">What we covered</h2>
<ul style="margin: 0 0 16px; padding-left: 22px;">
  <li style="margin-bottom: 6px;">[Neutral, factual recap point in client-appropriate language.]</li>
</ul>

<h2 style="color: #1a73e8; font-size: 18px; margin: 24px 0 8px;">What we agreed</h2>
<ul style="margin: 0 0 16px; padding-left: 22px;">
  <li>[Any decisions or agreements reached together. Omit this section entirely if none.]</li>
</ul>

<h2 style="color: #1a73e8; font-size: 18px; margin: 24px 0 8px;">Next steps</h2>
<table role="presentation" style="width: 100%; border-collapse: collapse; margin: 0 0 18px;">
  <tr style="background: #f8f9fa;"><td style="padding: 8px; border: 1px solid #ddd; width: 120px;"><strong>Us</strong></td><td style="padding: 8px; border: 1px solid #ddd;">[What WE committed to do for them, with timing if stated.]</td></tr>
  <tr><td style="padding: 8px; border: 1px solid #ddd;"><strong>You</strong></td><td style="padding: 8px; border: 1px solid #ddd;">[What THEY said they'd do, phrased as a friendly reminder. Omit row if none.]</td></tr>
</table>

<p>[Short, friendly closing inviting them to reply with questions.]</p>
<p>Best,<br>Shawn</p>
</body></html>
"""


# Deterministic content-safety scan for client-facing HTML: phrases that only
# appear when internal framing leaked into a client email. Cheap, non-LLM, final
# gate — the CLIENT_SPEC prompt is the first line of defense, this is the second.
_CLIENT_PROHIBITED = re.compile(
    r"\b(internal debrief|deal health|competitive intel|budget authority|"
    r"fireflies transcript|recording link)\b", re.IGNORECASE)


def client_safety_violation(html):
    """Return the offending phrase if client HTML fails the safety scan, else None."""
    m = _CLIENT_PROHIBITED.search(html or "")
    return m.group(0) if m else None


def _clean_html(text):
    out = (text or "").strip()
    # Keep only the email document when a model adds a preamble, a Markdown
    # fence, or trailing commentary. This does not rewrite any HTML attributes,
    # so the inline styles that email clients rely on remain byte-for-byte intact.
    start = re.search(r"(?is)<html\b", out)
    if start:
        out = out[start.start():]
    end = list(re.finditer(r"(?is)</html\s*>", out))
    if end:
        out = out[:end[-1].end()]

    # A truncated model response often contains a complete body but omits only
    # the final container tags. Repair that narrow case; the validator rejects
    # missing opening tags or malformed nesting rather than guessing.
    if re.match(r"(?is)^<html\b", out) and re.search(r"(?is)<body\b", out):
        if not re.search(r"(?is)</body\s*>", out):
            html_close = re.search(r"(?is)</html\s*>\s*$", out)
            if html_close:
                out = out[:html_close.start()] + "\n</body>\n" + out[html_close.start():]
            else:
                out += "\n</body>"
        if not re.search(r"(?is)</html\s*>\s*$", out):
            out += "\n</html>"
    return out


class _EmailHTMLValidator(HTMLParser):
    """Small strict validator for the intentionally limited recap markup."""

    _allowed = {
        "a", "body", "br", "h1", "h2", "h3", "hr", "html", "li", "ol",
        "p", "strong", "table", "tbody", "td", "th", "thead", "tr", "ul",
    }
    _void = {"br", "hr"}
    _parents = {
        "html": {None},
        "body": {"html"},
        "h1": {"body"}, "h2": {"body"}, "h3": {"body"},
        "hr": {"body"}, "p": {"body", "li", "td", "th"},
        "ul": {"body", "li", "td"}, "ol": {"body", "li", "td"},
        "li": {"ul", "ol"}, "table": {"body", "td"},
        "thead": {"table"}, "tbody": {"table"},
        "tr": {"table", "thead", "tbody"}, "td": {"tr"}, "th": {"tr"},
        "a": {"p", "li", "td", "th"}, "strong": {"p", "li", "td", "th", "a"},
        "br": {"p", "li", "td", "th"},
    }
    _attrs = {
        "a": {"href", "style"}, "body": {"style"}, "h1": {"style"},
        "h2": {"style"}, "h3": {"style"}, "hr": {"style"}, "li": {"style"},
        "ol": {"style"}, "p": {"style"}, "strong": {"style"},
        "table": {"role", "style"}, "tbody": {"style"}, "td": {"style"},
        "th": {"style"}, "thead": {"style"}, "tr": {"style"}, "ul": {"style"},
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.violation = None

    def _validate_tag(self, tag, attrs):
        tag = tag.lower()
        if tag not in self._allowed:
            self.violation = self.violation or f"unsupported <{tag}> tag"
            return tag
        parent = self.stack[-1] if self.stack else None
        if parent not in self._parents.get(tag, set()):
            self.violation = self.violation or f"invalid <{tag}> placement"
        allowed_attrs = self._attrs.get(tag, set())
        for name, value in attrs:
            name = name.lower()
            if name not in allowed_attrs:
                self.violation = self.violation or f"unsupported {name} attribute"
            if name == "href" and not re.match(r"(?is)^https?://", value or ""):
                self.violation = self.violation or "unsafe link URL"
            if name == "style" and re.search(
                    r"(?is)(?:expression\s*\(|url\s*\(|display\s*:\s*(?:flex|grid))",
                    value or ""):
                self.violation = self.violation or "unsupported email styling"
        return tag

    def handle_starttag(self, tag, attrs):
        tag = self._validate_tag(tag, attrs)
        if tag not in self._void:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        tag = self._validate_tag(tag, attrs)
        if tag not in self._void:
            self.violation = self.violation or f"unsupported self-closing <{tag}> tag"

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self._void:
            return
        if not self.stack or self.stack[-1] != tag:
            self.violation = self.violation or f"malformed <{tag}> nesting"
            return
        self.stack.pop()

    def result(self):
        if self.violation:
            return self.violation
        if self.stack:
            return f"unclosed <{self.stack[-1]}> tag"
        return None


def html_format_violation(html):
    """Return a deterministic email-format violation, or None when sendable."""
    value = (html or "").strip()
    if "```" in value:
        return "markdown fence"
    if not re.match(
            r"(?is)^<html\b[^>]*>\s*<body\b[^>]*>.*</body\s*>\s*</html\s*>$",
            value):
        return "incomplete html/body structure"
    for tag in ("html", "body"):
        if len(re.findall(fr"(?is)<{tag}\b", value)) != 1 or len(
                re.findall(fr"(?is)</{tag}\s*>", value)) != 1:
            return f"malformed {tag} structure"
    parser = _EmailHTMLValidator()
    parser.feed(value)
    parser.close()
    tag_violation = parser.result()
    if tag_violation:
        return tag_violation
    if re.search(r"(?is)\[[^\[\]]+\]", value):
        return "placeholder text"
    for listing in re.findall(r"(?is)<(?:ul|ol)\b[^>]*>(.*?)</(?:ul|ol)\s*>", value):
        items = re.findall(r"(?is)<li\b[^>]*>(.*?)</li\s*>", listing)
        if not any(_html_text(item) for item in items):
            return "empty list"
    for table in re.findall(r"(?is)<table\b[^>]*>(.*?)</table\s*>", value):
        cells = re.findall(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]\s*>", table)
        if not any(_html_text(cell) for cell in cells):
            return "empty table"

    owner = re.escape(_html_escape(OWNER_DISPLAY_NAME))
    signature = (
        rf"(?is)<p\b[^>]*>\s*Best,\s*<br\s*/?>\s*{owner}\s*</p>"
        rf"\s*</body\s*>\s*</html\s*>$"
    )
    if not re.search(signature, value):
        return "missing or malformed owner signature"
    return None


_CANNED_RECAP_PHRASES = re.compile(
    r"\b(i hope this (?:email|message) finds you well|it was great connecting|"
    r"thank you for (?:the )?productive discussion|important topics|"
    r"aligned on next steps|valuable insights)\b", re.IGNORECASE)
_ANCHOR_STOP_WORDS = {
    "about", "after", "again", "could", "discussion", "from", "have", "important",
    "into", "meeting", "next", "notes", "project", "recap", "shawn", "steps",
    "their", "there", "these", "they", "this", "today", "with", "would", "your",
}


def _html_text(value):
    text = re.sub(r"(?is)<[^>]+>", " ", value or "")
    return " ".join(html_lib.unescape(text).replace("\xa0", " ").split())


def _meeting_anchor_values(value, key=""):
    """Yield human meeting content while excluding transport metadata."""
    ignored = {"datestring", "duration", "email", "id", "transcript_url", "url"}
    if key.lower() in ignored:
        return
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from _meeting_anchor_values(child, str(child_key))
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _meeting_anchor_values(child, key)
    elif isinstance(value, str):
        yield value


def recap_voice_violation(html, transcript):
    """Reject canned copy or a recap with no concrete transcript anchor."""
    text = _html_text(html)
    canned = _CANNED_RECAP_PHRASES.search(text)
    if canned:
        return f"canned phrase {canned.group(0)!r}"
    source = " ".join(_meeting_anchor_values(transcript or {}))
    anchors = {
        token.lower() for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9'_-]{3,}", source)
        if token.lower() not in _ANCHOR_STOP_WORDS
    }
    output_tokens = {
        token.lower() for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9'_-]{3,}", text)
    }
    if anchors:
        matches = anchors.intersection(output_tokens)
        if not matches or (len(matches) < 2 and not any(len(word) >= 5 for word in matches)):
            return "no concrete meeting detail"
    return None


def _clean_json(text):
    out = (text or "").strip()
    if out.startswith("```"):
        lines = out.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        out = "\n".join(lines).strip()
    start = out.find("{")
    end = out.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("model returned no JSON object")
    payload = json.loads(out[start:end + 1])
    if not isinstance(payload, dict):
        raise ValueError("model JSON is not an object")
    return payload


def gen_argv(prompt):
    """The GEN_BIN command line for a prompt: `-z <prompt>` or, with
    RECAP_GEN_STDIN=1, `-z -` with the prompt delivered on stdin."""
    if GEN_STDIN:
        return [GEN_BIN, "-m", GEN_MODEL, "-z", "-"]
    return [GEN_BIN, "-m", GEN_MODEL, "-z", prompt]


def _gen_run(prompt, env):
    """One GEN_BIN invocation. Raises subprocess.TimeoutExpired like subprocess.run."""
    return subprocess.run(gen_argv(prompt), input=(prompt if GEN_STDIN else None),
                          capture_output=True, text=True, timeout=GEN_TIMEOUT, env=env)


def generate_json(prompt):
    """Run the configured model for structured analysis."""
    if not (os.path.exists(GEN_BIN) or _which(GEN_BIN)):
        raise RuntimeError(f"generation CLI '{GEN_BIN}' not found")
    sub_env = dict(os.environ)
    sub_env["HYPERSWARM_MEMORY_DISABLE"] = "1"
    last = ""
    for attempt in (1, 2):
        log(f"gen structured attempt {attempt} model={GEN_MODEL}")
        try:
            proc = _gen_run(prompt, sub_env)
        except subprocess.TimeoutExpired:
            last = f"timeout {GEN_TIMEOUT}s"
            continue
        if proc.returncode == 0:
            try:
                return _clean_json(proc.stdout)
            except (ValueError, json.JSONDecodeError) as exc:
                last = str(exc)
        else:
            last = f"rc={proc.returncode} stderr={proc.stderr[-300:]}"
        log(f"structured attempt {attempt} unusable: {last}")
    raise RuntimeError(f"structured generation failed: {last}")


def _html_escape(value):
    return (str(value or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


CLIENT_SPACER = "<p>&nbsp;</p>"


def space_client_recap(html):
    """Put one blank line after the client greeting and one before the signature.

    Client recaps open with "Hi <names>," and close with the owner signature. A
    paragraph margin alone renders as no gap in several mail clients, so the
    blank line is an explicit spacer paragraph. Deterministic and idempotent:
    it runs after enforce_owner_signature, so the signature it looks for is the
    one that function wrote.
    """
    out = html or ""
    greeting = re.search(r"(?is)<p[^>]*>\s*(hi|hello|hey)\b.*?</p>", out)
    if greeting and not out[greeting.end():].lstrip().startswith(CLIENT_SPACER):
        out = out[:greeting.end()] + "\n" + CLIENT_SPACER + out[greeting.end():]
    signature = f"<p>Best,<br>{_html_escape(OWNER_DISPLAY_NAME)}</p>"
    idx = out.rfind(signature)
    if idx != -1 and not out[:idx].rstrip().endswith(CLIENT_SPACER):
        out = out[:idx] + CLIENT_SPACER + "\n" + out[idx:]
    return out


def enforce_owner_signature(html):
    """Deterministically sign every recap from Shawn, never from another attendee."""
    out = _clean_html(html)
    signature = f"<p>Best,<br>{_html_escape(OWNER_DISPLAY_NAME)}</p>"
    body_match = re.search(r"(?is)</body>", out)
    if not body_match:
        return out + "\n" + signature

    before_body = out[:body_match.start()]

    def remove_signoff(match):
        inner = re.sub(r"(?is)^<p[^>]*>|</p>$", "", match.group(0))
        inner = re.sub(r"(?is)<br\s*/?>", "\n", inner)
        lines = [
            _html_text(line).strip(" ,")
            for line in inner.splitlines() if _html_text(line).strip(" ,")
        ]
        closings = {"best", "regards", "thanks", "thank you", "sincerely", "cheers"}
        normalized = lines[0].lower()
        dash_signoff = bool(re.match(r"^[–—-]\s*[^\W\d_][\w .'-]{0,40}$", normalized))
        if not lines or (normalized not in closings and not dash_signoff):
            return match.group(0)
        if dash_signoff or (len(lines) <= 2 and (len(lines) == 1 or len(lines[1].split()) <= 4)):
            return ""
        return match.group(0)

    without_signoffs = re.sub(r"(?is)<p\b[^>]*>.*?</p>", remove_signoff, before_body)
    return without_signoffs.rstrip() + "\n" + signature + "\n" + out[body_match.start():]


_FMT_LABEL = {
    "internal": "INTERNAL MEETING (standup/planning/retro) template",
    "sales": "EXTERNAL / SALES CALL — internal debrief template",
    "client": "INTERNAL MEETING template, written as the CLIENT-FACING recap described above,",
}


def build_prompt(fmt, transcript):
    """Build the generation prompt without exposing recordings to clients."""
    payload = {
        "title": transcript.get("title"),
        "dateString": transcript.get("dateString"),
        "duration": transcript.get("duration"),
        "transcript_url": transcript.get("transcript_url"),
        "meeting_attendees": transcript.get("meeting_attendees"),
        "summary": transcript.get("summary"),
        "sentences": transcript.get("sentences"),
    }
    client_brief = ""
    if fmt == "client":
        payload.pop("transcript_url", None)
        client_brief = f"{CLIENT_SPEC}\n========================================================\n"
    ctx = json.dumps(payload)[:90000]
    fmt_label = _FMT_LABEL[fmt]
    return (
        f"{writing_spec()}\n\n"
        f"========================================================\n"
        f"{client_brief}"
        f"You are the WRITING layer only. The system has already fetched this "
        f"transcript, decided the meeting type, and will handle ALL recipients "
        f"and sending. Ignore any instruction above about tools, Gmail, "
        f"recipients, drafts, or who to send to — that is NOT your job.\n\n"
        f"Produce ONLY the email HTML body for this meeting using the "
        f"{fmt_label} and the quality standards above. Output raw HTML "
        f"starting with <html> and ending with </html>. No preamble, no "
        f"commentary, no code fences, no To/From/Subject lines. Never invent "
        f"facts, owners, deadlines, or commitments. Omit unsupported details "
        f"and empty sections.\n\n"
        f"TRANSCRIPT (JSON):\n{ctx}"
    )


def generate_html(fmt, transcript):
    if not (os.path.exists(GEN_BIN) or _which(GEN_BIN)):
        sys.exit(f"ERROR: generation CLI '{GEN_BIN}' not found (set RECAP_GEN_BIN)")
    prompt = build_prompt(fmt, transcript)
    sub_env = dict(os.environ)
    last = ""
    for attempt in (1, 2):
        log(f"gen one-shot attempt {attempt} model={GEN_MODEL} fmt={fmt}")
        t0 = time.monotonic()
        try:
            proc = _gen_run(prompt, sub_env)
        except subprocess.TimeoutExpired:
            last = f"timeout {GEN_TIMEOUT}s"
            continue
        html = enforce_owner_signature(_clean_html(proc.stdout))
        violation = html_format_violation(html)
        if not violation and fmt == "client":
            violation = recap_voice_violation(html, transcript)
        if proc.returncode == 0 and not violation and len(html) > 200:
            log(f"gen ok ({time.monotonic()-t0:.1f}s, {len(html)} chars)")
            return html
        last = (f"rc={proc.returncode} len={len(html)} format={violation or 'too short'} "
                f"stderr={proc.stderr[-300:]}")
        log(f"attempt {attempt} unusable: {last}")
    sys.exit(f"ERROR: generation failed: {last}")


def _which(name):
    for p in os.environ.get("PATH", "").split(os.pathsep):
        cand = os.path.join(p, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


# ----- Composio Gmail (deterministic send) ------------------------------------
def _composio(tool, arguments):
    key = os.environ.get("COMPOSIO_API_KEY")
    uid = os.environ.get("COMPOSIO_USER_ID")
    if not (key and uid):
        return {"_error": "COMPOSIO_API_KEY/USER_ID missing"}
    body = {"user_id": uid, "arguments": arguments}
    # Name the mailbox. A Composio entity can hold several Gmail connections and
    # the default pick is silent; the REST execute route honours a top-level
    # connected_account_id, so pass it whenever the deployment sets one.
    ca = (os.environ.get("COMPOSIO_CONNECTED_ACCOUNT_ID") or "").strip()
    if ca:
        body["connected_account_id"] = ca
    r = _req("POST", f"{COMPOSIO_EXEC}/{tool}",
             {"x-api-key": key, "Content-Type": "application/json"}, body, timeout=90)
    if isinstance(r, dict) and r.get("_error"):
        # Loud in the spawn log. A dead key answered 401 on every send and every
        # fallback draft for two weeks (2026-08-21..09-03) and the log never said so.
        log(f"Composio {tool} failed: {str(r['_error'])[:300]}")
        return r
    if isinstance(r, dict) and r.get("successful") is False:
        # HTTP 200 with a tool-level failure. Counting this as delivered is how a
        # recap gets reported "✓ sent" with nothing in anyone's inbox.
        err = f"Composio {tool} not successful: {str(r.get('error'))[:300]}"
        log(err)
        return {"_error": err, "raw": r}
    return r


def send_email(to_list, subject, html):
    args = {"recipient_email": to_list[0], "subject": subject,
            "body": html, "is_html": True}
    if len(to_list) > 1:
        args["extra_recipients"] = to_list[1:]
    return _composio("GMAIL_SEND_EMAIL", args)


def create_draft(to, subject, html):
    to_list = [to] if isinstance(to, str) else list(to)
    if not to_list:
        return {"_error": "draft requires at least one recipient"}
    args = {"recipient_email": to_list[0], "subject": subject,
            "body": html, "is_html": True}
    if len(to_list) > 1:
        args["extra_recipients"] = to_list[1:]
    return _composio("GMAIL_CREATE_EMAIL_DRAFT", args)


def deliver_recap(descriptor):
    """Send internal mail; hold every client-facing recap as a Gmail draft."""
    delivery = create_draft if descriptor["kind"] == "client" else send_email
    return delivery(descriptor["recipients"], descriptor["subject"], descriptor["html"])


def notify(msg):
    """Optional status ping via the gen CLI's `send` verb. No-op if unconfigured."""
    if not NOTIFY_TARGET:
        log(f"status: {msg}")
        return
    try:
        subprocess.run([GEN_BIN, "send", "-t", NOTIFY_TARGET, "-q", msg],
                       timeout=60, capture_output=True, text=True)
    except Exception as e:
        log(f"notify failed: {e}")


def linear_enabled():
    return os.environ.get("RECAP_LINEAR_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def reconcile_linear(transcript, dry=False):
    """Best-effort Linear reconciliation; never fail the recap workflow."""
    if not linear_enabled():
        return {"completed": [], "created": [], "errors": [], "skipped": ["disabled"]}
    try:
        result = linear_recap.reconcile(transcript, LinearAPI(), generate_json, dry=dry)
        log(linear_recap.summarize(result))
        return result
    except Exception as exc:
        log(f"Linear reconciliation failed: {type(exc).__name__}: {exc}")
        return {"completed": [], "created": [],
                "errors": [f"{type(exc).__name__}: {exc}"], "skipped": []}


def print_linear_dry_run(result):
    print(f"--- {linear_recap.summarize(result)}")
    if result.get("preview"):
        print(json.dumps(result["preview"], indent=2, sort_keys=True))


# ----- subjects ---------------------------------------------------------------
def fmt_date(transcript):
    ds = transcript.get("dateString") or ""
    for f in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(ds, f).strftime("%B %-d, %Y")
        except Exception:
            pass
    return ds or "today"


def subject_for(fmt, transcript):
    """Internal and client recaps share one subject; only the debrief differs."""
    title = (transcript.get("title") or "Meeting").strip()
    d = fmt_date(transcript)
    if fmt == "sales":
        return f"{title} Call Recap – {d} | Intel + Next Steps"
    return f"{title} Recap & Reminders – {d} | Summary + Action Items"


_KIND_LABEL = {
    "internal": "internal recap",
    "sales_debrief": "internal debrief",
    "client": "client recap",
}


# ----- main -------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--meeting-id", required=True)
    ap.add_argument("--event", default="Transcription completed")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    load_env()
    mid = args.meeting_id.strip()

    # 1. idempotency — permanent ledger (survives restarts) ...
    if not args.dry and mid in ledger_load():
        log(f"already processed {mid} — no action")
        return
    # ... plus an ATOMIC claim so duplicate Fireflies events (transcribed +
    # summarized) for the same meeting can't both reach the send step.
    if not args.dry and not claim(mid):
        log(f"another process already claimed {mid} — no action")
        return

    # 2. fetch
    transcript, err = fetch_transcript(mid)

    # 3. classify
    if transcript is None:
        mode, fmt, recipients, reason = "ambiguous", "internal", [], f"Fireflies fetch failed: {err}"
        transcript = {"title": f"meeting {mid}"}
    else:
        mode, fmt, recipients, reason = classify(transcript)

    title = transcript.get("title") or mid
    usable = bool(transcript.get("sentences") or transcript.get("summary"))

    # Fireflies can record meetings Shawn was invited to, or where the bot joined
    # from his calendar, even when he never personally joined. Those should not
    # send recap emails or create owner drafts.
    if transcript is not None and not owner_joined_meeting(transcript):
        log(f"owner did not personally join \"{title}\" ({mid}) — recap disabled")
        if not args.dry:
            ledger_add(mid)
        return

    # ----- ambiguous: draft the recap to the owner, never auto-send ------------
    if mode == "ambiguous":
        subject = subject_for(fmt, transcript)
        html = (enforce_owner_signature(generate_html(fmt, transcript)) if usable else
                f"<html><body><p>Recap held for <b>{title}</b> (meeting {mid}). "
                f"Reason: {reason}. The transcript could not be used.</p></body></html>")
        if args.dry:
            print(f"--- MODE={mode} FMT={fmt} RECIPIENTS=[draft->{OWNER_EMAIL or 'OWNER'}] REASON={reason}")
            print(f"--- SUBJECT: {subject}")
            print(html)
            if usable:
                print_linear_dry_run(reconcile_linear(transcript, dry=True))
            return
        if not OWNER_EMAIL:
            log("ambiguous and no RECAP_OWNER_EMAIL set — nothing to draft to")
            if usable:
                notify(linear_recap.summarize(reconcile_linear(transcript)))
            return
        r = create_draft(OWNER_EMAIL, subject, html)
        ok = not (isinstance(r, dict) and r.get("_error"))
        linear_result = reconcile_linear(transcript) if usable else None
        linear_status = ("\n" + linear_recap.summarize(linear_result)) if linear_result else ""
        notify((f"DRAFT held — \"{title}\". Reason: {reason}. Review your Gmail drafts."
                if ok else f"Recap FAILED to draft — \"{title}\". {r}") + linear_status)
        if ok:
            ledger_add(mid)
        return

    # ----- internal / sales: build the send plan -------------------------------
    all_emails = attendee_emails(transcript)
    sends = apply_external_context_gate(
        route_sends(mode, recipients, all_emails), transcript)
    if mode == "sales" and meeting_has_blocked_external_context(transcript):
        log(f'client-facing recap blocked by meeting context for "{title}" ({mid})')

    # Safety invariant: the internal debrief must never reach an external address.
    external = [e for e in all_emails if not e.endswith("@" + INTERNAL_DOMAIN)]
    for s in sends:
        if s["kind"] in ("internal", "sales_debrief"):
            leaked = [e for e in s["recipients"] if e in external]
            if leaked:
                sys.exit(f"ERROR: internal recap would leak to external {leaked} — aborting")

    fmt_for_kind = {"internal": "internal", "sales_debrief": "sales", "client": "client"}
    subj_for_kind = {
        "internal": subject_for("internal", transcript),
        "sales_debrief": subject_for("sales", transcript),
        "client": subject_for("client", transcript),
    }
    if not usable:
        held = (f"<html><body><p>Recap held for <b>{title}</b> (meeting {mid}). "
                f"The transcript could not be used.</p></body></html>")
        if args.dry:
            print(f"--- MODE={mode} unusable transcript -> draft to {OWNER_EMAIL or 'OWNER'}")
            print(held)
            return
        if OWNER_EMAIL:
            create_draft(OWNER_EMAIL, subject_for(fmt, transcript), held)
        notify(f"Recap held (no transcript) — \"{title}\".")
        ledger_add(mid)
        return

    for s in sends:
        s["subject"] = subj_for_kind[s["kind"]]
        s["html"] = enforce_owner_signature(generate_html(fmt_for_kind[s["kind"]], transcript))
        if s["kind"] == "client":
            s["html"] = space_client_recap(s["html"])
            bad = client_safety_violation(s["html"])
            if bad and not args.dry:
                sys.exit(f"ERROR: client recap failed deterministic content-safety "
                         f"scan (matched {bad!r}) — no draft created")
            if bad:
                print(f"--- WARNING: client recap failed content-safety scan "
                      f"(matched {bad!r}) — a real run would abort")

    if args.dry:
        print(f"--- MODE={mode} FMT={fmt} REASON={reason}")
        excluded = client_draft_exclusion(all_emails) if mode == "sales" else None
        if excluded:
            print(f"--- no client draft: attendee at {excluded} (RECAP_NO_CLIENT_DRAFT_DOMAINS)")
        for s in sends:
            action = "DRAFT" if s["kind"] == "client" else "SEND"
            print(f"--- {action} kind={s['kind']} ({_KIND_LABEL[s['kind']]}) "
                  f"RECIPIENTS={s['recipients']}")
            print(f"    SUBJECT: {s['subject']}")
            print(s["html"])
        print_linear_dry_run(reconcile_linear(transcript, dry=True))
        return

    # ----- deliver internal mail; hold client-facing mail as drafts ------------
    results = []
    for s in sends:
        # Belt-and-suspenders: route_sends already strips blocked domains, but
        # re-filter at the send boundary so no future routing change can ever
        # address an automated recap to a blocked inbox.
        s["recipients"] = [e for e in s["recipients"] if not is_blocked_recipient(e)]
        if not s["recipients"]:
            continue
        r = deliver_recap(s)
        ok = not (isinstance(r, dict) and r.get("_error"))
        results.append((s, ok, r))
        if not ok and s["kind"] != "client" and OWNER_EMAIL:
            create_draft(OWNER_EMAIL, s["subject"], s["html"])

    # Ticket reconciliation happens after email delivery or draft creation and
    # is best effort. A Linear outage cannot suppress either artifact.
    linear_result = reconcile_linear(transcript)
    ledger_add(mid)  # claim already prevents re-fire; never re-run (would double-send the parts that worked)
    lines = []
    for s, ok, r in results:
        label = _KIND_LABEL[s["kind"]]
        if ok and s["kind"] == "client":
            lines.append(f"✓ {label} drafted → {', '.join(s['recipients'])}")
        elif ok:
            lines.append(f"✓ {label} sent → {', '.join(s['recipients'])}")
        elif s["kind"] == "client":
            lines.append(f"✗ {label} FAILED to draft: {r}")
        else:
            lines.append(f"✗ {label} FAILED (held an owner draft): {r}")
    lines.append(linear_recap.summarize(linear_result))
    notify(f"Recap for \"{title}\" ({mode}):\n" + "\n".join(lines))


if __name__ == "__main__":
    main()
