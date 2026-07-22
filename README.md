# fireflies-meeting-recap

Turn a finished [Fireflies](https://fireflies.ai) meeting into a recap email,
automatically. When Fireflies finishes transcribing, a webhook fires a small
deterministic Python driver that fetches the transcript, decides recipients by
email domain, writes the recap with an LLM, sends it through Gmail, and
reconciles explicit meeting work with Linear.

The interesting part is the recipient policy. The driver — not the model —
decides who gets what:

- **Internal meeting** (everyone is on your domain): one recap to all attendees.
- **External / sales call** (an outside guest is present): **two** emails —
  1. an **internal debrief** to your team only (the guest never receives it), and
  2. a **client-facing recap** to **everyone**, written from a separate,
     guard-railed template that carries no internal notes.
- **Ambiguous** (no attendee emails, an outside-only call, or a fetch failure):
  a draft is held for the owner; nothing is sent automatically.

A hard assertion before every send guarantees the internal debrief can never
reach an external address.

## Flow

```
Fireflies "transcription completed"
  └─POST→ https://<your-public-host>/webhooks/fireflies   (TLS via your tunnel/proxy)
            └→ receiver.py        (stdlib http.server, 127.0.0.1:8765)
                 └ spawns →  run_recap.py --meeting-id <id>     (returns 202 at once)
                              1. claim     atomic O_EXCL lock — dup webhooks can't double-send
                              2. dedupe    permanent ledger (survives restarts)
                              3. fetch     Fireflies GraphQL
                              4. classify  recipients/mode by email domain
                              5. generate  one-shot LLM CLI → recap HTML body only
                              6. send      Composio Gmail (send / draft)
                              7. reconcile search/update/create Linear work
                              8. notify    optional status ping
```

## Why the atomic claim matters

Fireflies fires **more than one event for the same meeting** (for example
`meeting.transcribed` and `meeting.summarized`), and the receiver spawns a
process per event. A naive dedupe ledger written *after* the slow
generate-and-send step doesn't help: the second event's process passes the
"already seen?" check before the first finishes, and you get **two emails**.
`claim()` takes an `O_CREAT|O_EXCL` lock file before any work, so the first
event wins and every duplicate bails immediately.

## Install

Requires Python 3.8+ (standard library only), a Fireflies API key, and a
[Composio](https://composio.dev) account with connected Gmail, plus a Linear
workspace API key.

```bash
git clone https://github.com/Screddyice/fireflies-meeting-recap.git
cd fireflies-meeting-recap

cp recap.env.example ~/secrets/recap.env
chmod 600 ~/secrets/recap.env
$EDITOR ~/secrets/recap.env          # fill in keys + RECAP_INTERNAL_DOMAIN

python3 -m unittest -v                # sanity check
```

Run the receiver (foreground, or via the included systemd user unit):

```bash
RECAP_ENV_FILE=~/secrets/recap.env python3 receiver.py
# or: cp meeting-recap-receiver.service.example ~/.config/systemd/user/meeting-recap-receiver.service
#     systemctl --user enable --now meeting-recap-receiver.service
```

Then set up the webhook (next section).

## Webhook setup

Fireflies webhook registration is dashboard-only: there is no API mutation for
it. Register the public URL once under **Integrations → Webhooks** at
https://app.fireflies.ai/integrations/custom/webhooks and treat it as
permanent. Every re-registration is a manual dashboard visit, so pick a URL
that never rotates (a stable hostname, not an ephemeral tunnel), and prefer
keeping it stable across infrastructure moves (see the relay setup below).

### Single host

The receiver binds `127.0.0.1:8765`. Expose it publicly with TLS using
whatever you prefer; with Tailscale Funnel:

```bash
sudo tailscale funnel --bg --set-path=/webhooks/fireflies http://127.0.0.1:8765
```

Register `https://<host>.<tailnet>.ts.net/webhooks/fireflies` in the Fireflies
dashboard. Funnel hostnames are permanent, so this survives restarts and
re-deploys.

### Split hosts: keep the registered URL, move the worker

When the recap service moves to another machine, keep the original host as a
dumb front door and relay to the new worker over the tailnet. Fireflies keeps
posting to the same URL and the dashboard never needs touching.

On the **worker** (runs receiver.py, loopback-bound), expose the port to the
tailnet only. The worker gets no public endpoint:

```bash
sudo tailscale serve --bg --tcp=8765 tcp://127.0.0.1:8765
```

On the **front door** (owns the registered URL), run a socat relay as a
systemd user unit:

```ini
# ~/.config/systemd/user/meeting-recap-relay.service
[Unit]
Description=Meeting Recap - relay Fireflies webhook to the worker (tailnet)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/socat TCP-LISTEN:8765,fork,reuseaddr,bind=127.0.0.1 TCP:<worker>.<tailnet>.ts.net:8765
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now meeting-recap-relay.service
sudo tailscale funnel --bg --set-path=/webhooks/fireflies http://127.0.0.1:8765
```

The public TLS leg terminates at the front door's Funnel; the front-door to
worker leg is plain HTTP inside the tailnet, encrypted in transit by
WireGuard. Disable any old receiver unit on the front door
(`systemctl --user disable --now meeting-recap-receiver.service`) so a reboot
cannot resurrect a second sender.

### Verify

Post a non-transcript event at the registered URL and confirm the worker saw
it:

```bash
curl -X POST -H 'Content-Type: application/json' \
  -d '{"meetingId":"SMOKE-1","eventType":"meeting.bot_joined"}' \
  https://<front-door>/webhooks/fireflies
# expect HTTP 202 {"status": "accepted", ... "ignored"}

journalctl --user -u meeting-recap-receiver.service -n 3   # on the worker
# expect: ignored non-transcript event meeting-id=SMOKE-1
```

The smoke event is ignored by design, so it proves routing without generating
or sending anything.

## Configuration

All configuration is environment variables (see
[`recap.env.example`](recap.env.example)):

| Variable | Purpose |
|---|---|
| `FIREFLIES_API_KEY` | Fireflies GraphQL auth |
| `COMPOSIO_API_KEY` / `COMPOSIO_USER_ID` | Gmail send/draft via Composio |
| `LINEAR_API_KEY` / `NEB_LINEAR_API_KEY` | Linear GraphQL access; NEB-prefixed value wins |
| `RECAP_INTERNAL_DOMAIN` | the domain that counts as "internal" |
| `RECAP_BLOCKED_DOMAINS` | domains that must never receive an automated recap (comma-separated) |
| `RECAP_EMAIL_ALIASES` | `alias=canonical` pairs for teammates on a second address |
| `RECAP_OWNER_EMAIL` | fallback inbox for drafts / failures |
| `RECAP_NOTIFY_TARGET` | optional status pings (blank to disable) |
| `RECAP_GEN_BIN` / `RECAP_GEN_MODEL` | the generation CLI and model |
| `RECAP_WRITING_SPEC` | path to the recap writing guidance |
| `RECAP_LINEAR_ENABLED` | Linear reconciliation kill switch; defaults to enabled |

### Recipient safety

Three deterministic (non-LLM) gates run on every send:

- **Blocked domains.** Addresses at a `RECAP_BLOCKED_DOMAINS` domain are
  stripped from every route, and re-filtered again at the send boundary as a
  belt-and-suspenders check. Classification still sees the true attendee list,
  so a call with a blocked guest still produces the internal debrief for your
  team — but no automated mail is ever addressed to a blocked inbox, and if the
  only guest was blocked, no client recap is sent at all. Use this for clients
  under a no-automation agreement.
- **Email aliases.** `RECAP_EMAIL_ALIASES` canonicalizes teammates who join
  under a second address (an agency account, a personal calendar) to their
  internal identity, so those meetings classify as internal instead of leaking
  an internal debrief route to an "external" address. Alias specific people,
  never whole domains.
- **Client content scan.** Client-facing HTML is checked against a prohibited
  phrase list (internal debrief, deal health, competitive intel, transcript
  links, ...) after generation; a real run aborts before anything is sent if the
  scan matches, and `--dry` prints a warning.

## Linear reconciliation

Every usable transcript gets a separate structured analysis after the recap is
prepared. The system first loads the live Linear teams, projects, memberships,
and users. It extracts only two kinds of work:

- explicitly completed work that may match an existing issue;
- explicit future commitments with an internal owner.

It searches Linear before any write, then runs a second match adjudication. A
completion is allowed only when the selected issue came from that claim's
search results, the transcript contains the proposed evidence verbatim, the
issue is still open, and its title strongly matches the completed work. The
issue is fetched again immediately before moving it to that team's completed
state.

New issues are minimized by merging related commitments during extraction and
searching each proposed title before creation. Because a duplicate is usually a
paraphrase rather than a text match, the candidate pool for every proposed task
also includes the open issues of its target team (the 100 most recently
updated, terminal states excluded), so the adjudicator compares the proposal
against the team's real backlog and suppresses tickets that describe the same
deliverable in different words. Open title matches are suppressed even if the
model misses the duplicate. Team, project, assignee membership,
priority, and due date are validated in Python; unclear owners or cross-team
projects are skipped instead of creating loose tickets. Projects are the Linear
"folder" used when the transcript clearly maps to one; otherwise the issue is
placed in the resolved team without guessing a project.

Linear failures are reported in the status notification but never block recap
email delivery. `--dry` performs reads and prints the validated Linear plan but
never calls an update or create tool.

### Bring your own LLM

Generation is a pluggable CLI invoked as `<bin> -m <model> -z "<prompt>"` that
prints the email HTML to stdout. Point `RECAP_GEN_BIN` at any wrapper around the
model you want. The internal/sales writing guidance lives in
[`templates/writing_spec.md`](templates/writing_spec.md) — edit it to match your
team's voice. The client-facing template is `CLIENT_SPEC` in `run_recap.py`,
kept separate on purpose so internal framing can't leak into a client email.

## Test a meeting without sending

```bash
python3 run_recap.py --meeting-id <FIREFLIES_MEETING_ID> --dry
```

`--dry` fetches, classifies, and generates, then prints the full email and
validated Linear plans. It sends no email and performs no Linear writes.

## Tests

```bash
python3 -m unittest -v
```

Covers classification, recipient routing (including the no-external-leak
invariant), atomic claims, completion evidence/candidate gates, duplicate
suppression, team/project/user resolution, due dates, and dry-run write safety.

## Status

Single operator per deployment: one Fireflies key, one Gmail, one internal
domain. Multi-tenant onboarding (a deploy per teammate) is not built yet.

## License

MIT — see [LICENSE](LICENSE).
