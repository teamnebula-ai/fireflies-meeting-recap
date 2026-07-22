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
  - External/sales (>=1 outside attendee) → TWO emails:
      * internal debrief  → internal attendees ONLY (never the outside guest)
      * client recap      → ALL participants, using a separate client-safe template
  - Ambiguous (no emails, or outside guests but zero internal recipient, or fetch
    failed) → draft to RECAP_OWNER_EMAIL, no auto-send.

Configuration is entirely via environment variables (see recap.env.example).

Usage:
  run_recap.py --meeting-id <id> [--event "Transcription completed"] [--dry]
    --dry : fetch + classify + generate + print; send nothing; don't claim/mark seen.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
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

# Identity / routing
INTERNAL_DOMAIN = os.environ.get("RECAP_INTERNAL_DOMAIN", "example.com")
OWNER_EMAIL = os.environ.get("RECAP_OWNER_EMAIL", "")        # fallback/ambiguous draft target
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

# Teammates who join meetings under a second address (e.g. an agency account)
# but are internal. Alias SPECIFIC people, not whole domains — aliasing a domain
# would classify a genuine guest at that domain as internal.
EMAIL_ALIASES = _parse_aliases(os.environ.get("RECAP_EMAIL_ALIASES", ""))

FIREFLIES_GQL = "https://api.fireflies.ai/graphql"
COMPOSIO_EXEC = "https://backend.composio.dev/api/v3/tools/execute"


def log(msg):
    sys.stderr.write(f"[recap] {msg}\n")
    sys.stderr.flush()


def load_env():
    """Source RECAP_ENV_FILE into os.environ (KEY=VALUE, ignore #/blank)."""
    global GEN_BIN, GEN_MODEL, GEN_TIMEOUT, INTERNAL_DOMAIN, OWNER_EMAIL
    global NOTIFY_TARGET, WRITING_SPEC, BLOCKED_DOMAINS, EMAIL_ALIASES
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
    INTERNAL_DOMAIN = os.environ.get("RECAP_INTERNAL_DOMAIN", INTERNAL_DOMAIN)
    OWNER_EMAIL = os.environ.get("RECAP_OWNER_EMAIL", OWNER_EMAIL)
    NOTIFY_TARGET = os.environ.get("RECAP_NOTIFY_TARGET", NOTIFY_TARGET)
    WRITING_SPEC = Path(os.environ.get("RECAP_WRITING_SPEC", str(WRITING_SPEC)))
    BLOCKED_DOMAINS = _parse_domains(os.environ.get("RECAP_BLOCKED_DOMAINS", ""))
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
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        LEDGER.write_text(json.dumps(seen))


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
    d.mkdir(parents=True, exist_ok=True)
    p = d / (mid.replace("/", "_") + ".lock")
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, str(time.time()).encode())
        os.close(fd)
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
         "summary { overview action_items keywords bullet_gist outline } "
         "sentences { speaker_name text } } }")
    r = _req("POST", FIREFLIES_GQL,
             {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
             {"query": q, "variables": {"id": meeting_id}}, timeout=90)
    if isinstance(r, dict) and r.get("_error"):
        return None, r["_error"]
    t = (r or {}).get("data", {}).get("transcript")
    if not t:
        return None, f"transcript null (resp: {str(r)[:200]})"
    return t, None


# ----- classification (deterministic) -----------------------------------------
def attendee_emails(transcript):
    """De-duped, lowercased list of every attendee email on the transcript."""
    atts = transcript.get("meeting_attendees") or []
    emails = []
    for a in atts:
        e = canonical_email((a.get("email") or "").strip().lower())
        if e and "@" in e and e not in emails:
            emails.append(e)
    return emails


def canonical_email(email):
    """Map an aliased teammate address to their canonical internal identity."""
    return EMAIL_ALIASES.get(email, email)


def is_blocked_recipient(email):
    domain = (email.split("@", 1)[1] if "@" in email else "").lower()
    return domain in BLOCKED_DOMAINS


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
    """Pure recipient routing. Returns a list of {kind, recipients} send descriptors.

    kind:
      'internal'      -> internal-meeting recap, to all (internal) attendees
      'sales_debrief' -> internal debrief (summary/next-steps), INTERNAL ONLY
      'client'        -> client-facing recap, to ALL participants incl. the guest

    Safety invariant (enforced here, asserted by tests): a 'sales_debrief' send
    NEVER contains an external address. The only thing that reaches outside
    guests is the 'client' send, which carries the client-safe HTML.

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
        client = sendable(all_emails)
        # Only send a client recap if a genuine external recipient remains after
        # filtering — a call whose only guest was blocked has no client to recap to.
        if any(not e.endswith("@" + INTERNAL_DOMAIN) for e in client):
            sends.append({"kind": "client", "recipients": client})
        return sends
    return []  # ambiguous -> handled as a draft to the owner, not an auto-send


# ----- generation (LLM layer only) --------------------------------------------
def writing_spec():
    """The internal/sales writing guidance (templates + standards)."""
    try:
        return WRITING_SPEC.read_text()
    except Exception as e:
        sys.exit(f"ERROR: cannot read writing spec {WRITING_SPEC}: {e}")


CLIENT_SPEC = """\
You are writing a CLIENT-FACING meeting recap. This email is sent to EVERYONE who
was on the call, INCLUDING the external guest/client. It is from the meeting host
to the people they just met with.

ABSOLUTE RULES — the reader is the client. NEVER include any of the following:
- Internal intel, "deal health", deal stage, or how the call "felt".
- Budget speculation, pricing strategy, or guesses about their spend.
- Competitive intel or mentions of other vendors they're evaluating.
- Candid/internal assessments, "what didn't land", red flags, or buying signals.
- Internal-only tasks (CRM updates, "build a demo for their use case", research).
- The Fireflies transcript/recording link (that is internal only).
Write warm, professional, concise. Use "we" for our side and "you" for the client.

Use exactly this HTML structure:

<html><body style="font-family: Arial, sans-serif; color: #333; line-height: 1.7; max-width: 800px;">
<p>Hi [first names of the client attendees, comma-separated],</p>
<p>[1-2 warm sentences thanking them for their time and stating the headline of the conversation.]</p>

<h2 style="color: #1a73e8;">What we covered</h2>
<ul>
  <li>[Neutral, factual recap point — what was discussed, in client-appropriate language.]</li>
  <li>[Another point...]</li>
</ul>

<h2 style="color: #1a73e8;">What we agreed</h2>
<ul>
  <li>[Any decisions or agreements reached together. Omit this section entirely if none.]</li>
</ul>

<h2 style="color: #1a73e8;">Next steps</h2>
<table style="width: 100%; border-collapse: collapse; margin-bottom: 16px;">
  <tr style="background: #f8f9fa;"><td style="padding: 8px; border: 1px solid #ddd; width: 120px;"><strong>Us</strong></td><td style="padding: 8px; border: 1px solid #ddd;">[What WE committed to do for them, with timing if stated.]</td></tr>
  <tr><td style="padding: 8px; border: 1px solid #ddd;"><strong>You</strong></td><td style="padding: 8px; border: 1px solid #ddd;">[What THEY said they'd do, phrased as a friendly reminder. Omit row if none.]</td></tr>
</table>

<p>[Short, friendly closing inviting them to reply with questions.]</p>
<p>Best,<br>[Meeting host name]</p>
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
    if out.startswith("```"):
        lines = out.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        out = "\n".join(lines).strip()
    return out


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
            proc = subprocess.run([GEN_BIN, "-m", GEN_MODEL, "-z", prompt],
                                  capture_output=True, text=True,
                                  timeout=GEN_TIMEOUT, env=sub_env)
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


def generate_html(fmt, transcript):
    if not (os.path.exists(GEN_BIN) or _which(GEN_BIN)):
        sys.exit(f"ERROR: generation CLI '{GEN_BIN}' not found (set RECAP_GEN_BIN)")
    payload = {
        "title": transcript.get("title"),
        "dateString": transcript.get("dateString"),
        "duration": transcript.get("duration"),
        "transcript_url": transcript.get("transcript_url"),
        "meeting_attendees": transcript.get("meeting_attendees"),
        "summary": transcript.get("summary"),
        "sentences": transcript.get("sentences"),
    }
    ctx = json.dumps(payload)[:90000]
    if fmt == "client":
        prompt = (
            f"{CLIENT_SPEC}\n\n"
            f"========================================================\n"
            f"Produce ONLY the email HTML body. Output raw HTML starting with "
            f"<html> and ending with </html>. No preamble, no commentary, no "
            f"code fences, no To/From/Subject lines. Never invent facts, owners, "
            f"deadlines, or commitments.\n\n"
            f"TRANSCRIPT (JSON):\n{ctx}"
        )
    else:
        fmt_label = ("INTERNAL MEETING (standup/planning/retro)" if fmt == "internal"
                     else "EXTERNAL / SALES CALL — internal debrief")
        prompt = (
            f"{writing_spec()}\n\n"
            f"========================================================\n"
            f"You are the WRITING layer only. The system has already fetched this "
            f"transcript, decided the meeting type, and will handle ALL recipients "
            f"and sending. Ignore any instruction above about tools, Gmail, "
            f"recipients, drafts, or who to send to — that is NOT your job.\n\n"
            f"Produce ONLY the email HTML body for this meeting using the "
            f"{fmt_label} template and the quality standards above. Output raw HTML "
            f"starting with <html> and ending with </html>. No preamble, no "
            f"commentary, no code fences, no To/From/Subject lines. Never invent "
            f"facts, owners, deadlines, or commitments — if an owner or deadline "
            f"was not stated, write \"Not stated\".\n\n"
            f"TRANSCRIPT (JSON):\n{ctx}"
        )
    sub_env = dict(os.environ)
    last = ""
    for attempt in (1, 2):
        log(f"gen one-shot attempt {attempt} model={GEN_MODEL} fmt={fmt}")
        t0 = time.monotonic()
        try:
            proc = subprocess.run([GEN_BIN, "-m", GEN_MODEL, "-z", prompt],
                                  capture_output=True, text=True,
                                  timeout=GEN_TIMEOUT, env=sub_env)
        except subprocess.TimeoutExpired:
            last = f"timeout {GEN_TIMEOUT}s"
            continue
        html = _clean_html(proc.stdout)
        if proc.returncode == 0 and "<html" in html.lower() and len(html) > 200:
            log(f"gen ok ({time.monotonic()-t0:.1f}s, {len(html)} chars)")
            return html
        last = f"rc={proc.returncode} len={len(html)} stderr={proc.stderr[-300:]}"
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
    return _req("POST", f"{COMPOSIO_EXEC}/{tool}",
                {"x-api-key": key, "Content-Type": "application/json"},
                {"user_id": uid, "arguments": arguments}, timeout=90)


def send_email(to_list, subject, html):
    args = {"recipient_email": to_list[0], "subject": subject,
            "body": html, "is_html": True}
    if len(to_list) > 1:
        args["extra_recipients"] = to_list[1:]
    return _composio("GMAIL_SEND_EMAIL", args)


def create_draft(to, subject, html):
    return _composio("GMAIL_CREATE_EMAIL_DRAFT",
                     {"recipient_email": to, "subject": subject,
                      "body": html, "is_html": True})


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
    title = (transcript.get("title") or "Meeting").strip()
    d = fmt_date(transcript)
    if fmt == "sales":
        return f"{title} Call Recap – {d} | Intel + Next Steps"
    return f"{title} Recap & Reminders – {d} | Summary + Action Items"


def client_subject_for(transcript):
    title = (transcript.get("title") or "Our meeting").strip()
    return f"Recap & next steps: {title} – {fmt_date(transcript)}"


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

    # ----- ambiguous: draft the recap to the owner, never auto-send ------------
    if mode == "ambiguous":
        subject = subject_for(fmt, transcript)
        html = (generate_html(fmt, transcript) if usable else
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
    sends = route_sends(mode, recipients, all_emails)

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
        "client": client_subject_for(transcript),
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
        s["html"] = generate_html(fmt_for_kind[s["kind"]], transcript)
        if s["kind"] == "client":
            bad = client_safety_violation(s["html"])
            if bad and not args.dry:
                sys.exit(f"ERROR: client recap failed deterministic content-safety "
                         f"scan (matched {bad!r}) — nothing sent")
            if bad:
                print(f"--- WARNING: client recap failed content-safety scan "
                      f"(matched {bad!r}) — a real run would abort")

    if args.dry:
        print(f"--- MODE={mode} FMT={fmt} REASON={reason}")
        for s in sends:
            print(f"--- SEND kind={s['kind']} ({_KIND_LABEL[s['kind']]}) "
                  f"RECIPIENTS={s['recipients']}")
            print(f"    SUBJECT: {s['subject']}")
            print(s["html"])
        print_linear_dry_run(reconcile_linear(transcript, dry=True))
        return

    # ----- send each descriptor; fail-safe to an owner draft on any error ------
    results = []
    for s in sends:
        # Belt-and-suspenders: route_sends already strips blocked domains, but
        # re-filter at the send boundary so no future routing change can ever
        # address an automated recap to a blocked inbox.
        s["recipients"] = [e for e in s["recipients"] if not is_blocked_recipient(e)]
        if not s["recipients"]:
            continue
        r = send_email(s["recipients"], s["subject"], s["html"])
        ok = not (isinstance(r, dict) and r.get("_error"))
        results.append((s, ok, r))
        if not ok and OWNER_EMAIL:
            create_draft(OWNER_EMAIL, s["subject"], s["html"])

    # Ticket reconciliation happens after email delivery and is best effort. A
    # Linear outage or model failure can never suppress a recap email.
    linear_result = reconcile_linear(transcript)
    ledger_add(mid)  # claim already prevents re-fire; never re-run (would double-send the parts that worked)
    lines = []
    for s, ok, r in results:
        label = _KIND_LABEL[s["kind"]]
        lines.append(f"✓ {label} → {', '.join(s['recipients'])}" if ok
                     else f"✗ {label} FAILED (held a draft): {r}")
    lines.append(linear_recap.summarize(linear_result))
    notify(f"Recap for \"{title}\" ({mode}):\n" + "\n".join(lines))


if __name__ == "__main__":
    main()
