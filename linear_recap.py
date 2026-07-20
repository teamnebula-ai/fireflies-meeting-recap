"""Safe Linear reconciliation for a completed meeting transcript.

The model proposes and adjudicates work.  This module owns every side effect and
only permits writes after validating the proposal against data returned by
Linear and verbatim evidence from the Fireflies transcript.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone


LIST_TEAMS = "LINEAR_LIST_LINEAR_TEAMS"
LIST_PROJECTS = "LINEAR_LIST_LINEAR_PROJECTS"
LIST_USERS = "LINEAR_LIST_LINEAR_USERS"
SEARCH_ISSUES = "LINEAR_SEARCH_ISSUES"
GET_ISSUE = "LINEAR_GET_LINEAR_ISSUE"
LIST_STATES = "LINEAR_LIST_LINEAR_STATES"
UPDATE_ISSUE = "LINEAR_UPDATE_ISSUE"
CREATE_ISSUE = "LINEAR_CREATE_LINEAR_ISSUE"

MAX_ACTIONS = 8
_TERMINAL_TYPES = {"completed", "canceled", "cancelled"}
_STOPWORDS = {
    "a", "an", "and", "for", "in", "of", "on", "the", "to", "with",
    "add", "build", "create", "finish", "implement", "make", "update",
}


def _data(response):
    """Return the useful Composio payload or raise a concise runtime error."""
    if not isinstance(response, dict):
        raise RuntimeError("Composio returned a non-object response")
    if response.get("_error"):
        raise RuntimeError(str(response["_error"]))
    if response.get("successful") is False:
        raise RuntimeError(str(response.get("error") or response.get("data") or response))
    payload = response.get("data", response)
    if isinstance(payload, dict) and payload.get("_error"):
        raise RuntimeError(str(payload["_error"]))
    return payload if isinstance(payload, dict) else {}


def _page_info(payload):
    return payload.get("page_info") or payload.get("pageInfo") or {}


def _paginate(call, tool, collection, arguments=None):
    args = dict(arguments or {})
    args.setdefault("first", 50)
    items = []
    for _ in range(20):
        payload = _data(call(tool, args))
        page = payload.get(collection) or payload.get("items") or []
        if isinstance(page, list):
            items.extend(item for item in page if isinstance(item, dict))
        info = _page_info(payload)
        if not info.get("hasNextPage"):
            break
        cursor = info.get("endCursor")
        if not cursor:
            raise RuntimeError(f"{tool} reported another page without a cursor")
        args["after"] = cursor
    return items


def workspace_catalog(call):
    """Fetch teams, projects and active human users, with membership edges."""
    teams = _paginate(call, LIST_TEAMS, "teams", {"first": 50})
    projects = _paginate(call, LIST_PROJECTS, "projects", {"first": 50})
    users = _paginate(call, LIST_USERS, "users", {"first": 50})

    project_by_id = {p.get("id"): dict(p) for p in projects if p.get("id")}
    for team in teams:
        team_id = team.get("id")
        for ref in team.get("projects") or []:
            project_id = ref.get("id") if isinstance(ref, dict) else ref
            if project_id in project_by_id:
                project_by_id[project_id].setdefault("team_ids", []).append(team_id)

    active_users = []
    for user in users:
        email = str(user.get("email") or "").lower()
        if user.get("active") is False or email.endswith("@oauthapp.linear.app") or email.endswith("@linear.linear.app"):
            continue
        active_users.append(user)

    return {
        "teams": teams,
        "projects": list(project_by_id.values()),
        "users": active_users,
    }


def _transcript_text(transcript):
    parts = []
    summary = transcript.get("summary") or {}
    if isinstance(summary, dict):
        for value in summary.values():
            if isinstance(value, list):
                parts.extend(str(v) for v in value)
            elif value:
                parts.append(str(value))
    for sentence in transcript.get("sentences") or []:
        if isinstance(sentence, dict) and sentence.get("text"):
            parts.append(str(sentence["text"]))
    return "\n".join(parts)


def _norm(value):
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def evidence_supported(evidence, transcript_text):
    evidence_norm = _norm(evidence)
    return len(evidence_norm) >= 12 and evidence_norm in _norm(transcript_text)


def title_overlap(left, right):
    def tokens(value):
        return {t for t in _norm(value).split() if len(t) > 2 and t not in _STOPWORDS}
    a, b = tokens(left), tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _catalog_for_model(catalog):
    project_names = {p.get("id"): p.get("name") for p in catalog["projects"]}
    return {
        "teams": [
            {
                "name": t.get("name"),
                "projects": [project_names.get(p.get("id")) for p in (t.get("projects") or [])
                             if isinstance(p, dict) and project_names.get(p.get("id"))],
                "members": [
                    {"name": m.get("name"), "email": m.get("email")}
                    for m in (t.get("members") or [])
                    if isinstance(m, dict) and m.get("email")
                ],
            }
            for t in catalog["teams"]
        ],
        "users": [
            {"name": u.get("name"), "display_name": u.get("displayName"), "email": u.get("email")}
            for u in catalog["users"]
        ],
    }


def _meeting_date(transcript):
    raw = transcript.get("dateString") or transcript.get("date")
    if isinstance(raw, (int, float)):
        value = float(raw)
        if value > 10_000_000_000:
            value /= 1000
        return datetime.fromtimestamp(value, tz=timezone.utc).date()
    if raw:
        text = str(raw).replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).date()
        except ValueError:
            try:
                return date.fromisoformat(str(raw)[:10])
            except ValueError:
                pass
    return datetime.now(timezone.utc).date()


def _extract_prompt(transcript, catalog):
    payload = {
        "title": transcript.get("title"),
        "meeting_date": _meeting_date(transcript).isoformat(),
        "attendees": transcript.get("meeting_attendees") or [],
        "summary": transcript.get("summary") or {},
        "sentences": transcript.get("sentences") or [],
    }
    return f"""You extract Linear work from a completed meeting. Return JSON only.

Use this exact schema:
{{"completion_claims":[{{"completed_work":"short description","search_query":"3-8 distinctive words","evidence":"verbatim transcript quote"}}],"future_tasks":[{{"title":"imperative outcome","description":"what done means and meeting context","team_name":"exact catalog team","project_name":"exact catalog project or empty","assignee_email":"exact active user email","due_date":"YYYY-MM-DD or empty","priority":"urgent|high|normal|low","evidence":"verbatim transcript quote"}}]}}

Rules:
- A completion claim requires explicit past-tense evidence that the work is finished, shipped, sent, resolved, or otherwise done. Discussion, progress, intent, approval, and vague status are not completion.
- A future task requires an explicit commitment or clearly assigned next action. Do not turn ideas, observations, client-owned work, or discussion points into tickets.
- Create the smallest efficient set. Merge related steps that share one outcome, owner, team/project, and due date. Maximum {MAX_ACTIONS} future tasks.
- Assign only an active user in the catalog who personally owns the action. If ownership or destination team is unclear, omit the task.
- Choose the narrowest matching project. Leave project_name empty if no catalog project clearly fits.
- Preserve an explicit date. Otherwise choose a realistic due date from urgency and dependencies relative to the meeting date.
- Evidence must be copied verbatim from the transcript, not paraphrased.

LINEAR CATALOG:
{json.dumps(_catalog_for_model(catalog), ensure_ascii=False)}

MEETING:
{json.dumps(payload, ensure_ascii=False)[:90000]}
"""


def _adjudication_prompt(intents, candidate_groups, transcript):
    compact_groups = []
    for group in candidate_groups:
        compact_groups.append({
            "kind": group["kind"],
            "intent_index": group["intent_index"],
            "intent": group["intent"],
            "candidates": [
                {
                    "id": c.get("id"), "identifier": c.get("identifier"),
                    "title": c.get("title"), "team": (c.get("team") or {}).get("name"),
                    "project": (c.get("project") or {}).get("name"),
                    "state": c.get("state"), "assignee": c.get("assignee"),
                }
                for c in group["candidates"]
            ],
        })
    return f"""Adjudicate proposed meeting actions against searched Linear candidates. Return JSON only.

Schema:
{{"complete":[{{"claim_index":0,"candidate_id":"UUID","confidence":"high","reason":"why it is the same work"}}],"create":[{{"task_index":0,"duplicate_candidate_id":"UUID or empty","reason":"why create or suppress"}}]}}

Rules:
- Complete only an exact semantic match for explicitly completed work. Never select related, parent, umbrella, or merely similar work. Use only candidate IDs shown for that completion claim. Require high confidence.
- For each future task, return one create decision. Set duplicate_candidate_id when an open candidate already represents substantially the same deliverable; otherwise leave it empty.
- Never invent an ID or task index.

PROPOSALS:
{json.dumps(intents, ensure_ascii=False)}

SEARCHED CANDIDATES:
{json.dumps(compact_groups, ensure_ascii=False)}

MEETING TITLE: {transcript.get('title') or ''}
"""


def _search(call, query):
    payload = _data(call(SEARCH_ISSUES, {
        "query": str(query).strip()[:200], "first": 15, "include_archived": False,
    }))
    issues = payload.get("issues") or payload.get("items") or []
    return [item for item in issues if isinstance(item, dict)]


def build_plan(transcript, call, model):
    """Build and validate a no-side-effect reconciliation plan."""
    catalog = workspace_catalog(call)
    transcript_text = _transcript_text(transcript)
    raw = model(_extract_prompt(transcript, catalog))
    intents = raw if isinstance(raw, dict) else json.loads(raw)
    claims = intents.get("completion_claims") or []
    tasks = intents.get("future_tasks") or []
    if not isinstance(claims, list) or not isinstance(tasks, list):
        raise RuntimeError("Linear extraction returned invalid collections")
    claims = [c for c in claims[:MAX_ACTIONS] if isinstance(c, dict)]
    tasks = [t for t in tasks[:MAX_ACTIONS] if isinstance(t, dict)]
    intents = {"completion_claims": claims, "future_tasks": tasks}

    groups = []
    for index, claim in enumerate(claims):
        query = claim.get("search_query") or claim.get("completed_work")
        candidates = _search(call, query) if query else []
        groups.append({"kind": "completion", "intent_index": index, "intent": claim,
                       "candidates": candidates})
    for index, task in enumerate(tasks):
        candidates = _search(call, task.get("title")) if task.get("title") else []
        groups.append({"kind": "future", "intent_index": index, "intent": task,
                       "candidates": candidates})

    raw_decisions = model(_adjudication_prompt(intents, groups, transcript))
    decisions = raw_decisions if isinstance(raw_decisions, dict) else json.loads(raw_decisions)
    return validate_plan(intents, decisions, groups, catalog, transcript_text,
                         _meeting_date(transcript))


def _exact_name(items, supplied, fields=("name",)):
    target = _norm(supplied)
    if not target:
        return None
    matches = [item for item in items if any(_norm(item.get(field)) == target for field in fields)]
    return matches[0] if len(matches) == 1 else None


def _resolve_user(catalog, email):
    target = str(email or "").strip().lower()
    matches = [u for u in catalog["users"] if str(u.get("email") or "").lower() == target]
    return matches[0] if len(matches) == 1 else None


def _member_of(team, user):
    user_id = user.get("id")
    return any(isinstance(member, dict) and member.get("id") == user_id
               for member in (team.get("members") or []))


def _priority(value):
    return {"urgent": 1, "high": 2, "normal": 3, "low": 4}.get(str(value).lower(), 3)


def sensible_due_date(raw, priority, meeting_date):
    try:
        parsed = date.fromisoformat(str(raw)[:10]) if raw else None
    except ValueError:
        parsed = None
    if parsed and meeting_date <= parsed <= meeting_date + timedelta(days=366):
        return parsed.isoformat()
    days = {1: 2, 2: 7, 3: 14, 4: 30}[priority]
    return (meeting_date + timedelta(days=days)).isoformat()


def validate_plan(intents, decisions, groups, catalog, transcript_text, meeting_date):
    """Convert model decisions into write-ready actions using hard gates."""
    completions = []
    creations = []
    skipped = []
    completion_groups = {g["intent_index"]: g for g in groups if g["kind"] == "completion"}
    future_groups = {g["intent_index"]: g for g in groups if g["kind"] == "future"}

    for decision in (decisions.get("complete") or [])[:MAX_ACTIONS]:
        if not isinstance(decision, dict) or decision.get("confidence") != "high":
            continue
        index = decision.get("claim_index")
        group = completion_groups.get(index)
        if not group:
            continue
        claim = group["intent"]
        evidence = claim.get("evidence")
        candidate = next((c for c in group["candidates"]
                          if c.get("id") == decision.get("candidate_id")), None)
        if not candidate or not evidence_supported(evidence, transcript_text):
            continue
        state_type = str((candidate.get("state") or {}).get("type") or "").lower()
        if state_type in _TERMINAL_TYPES:
            continue
        identifier = str(candidate.get("identifier") or "")
        overlap = title_overlap(claim.get("completed_work"), candidate.get("title"))
        if identifier.lower() not in transcript_text.lower() and overlap < 0.5:
            continue
        completions.append({
            "issue_id": candidate.get("id"), "identifier": identifier,
            "title": candidate.get("title"), "team_id": (candidate.get("team") or {}).get("id"),
            "evidence": evidence,
        })

    create_decisions = {}
    for decision in decisions.get("create") or []:
        if isinstance(decision, dict) and isinstance(decision.get("task_index"), int):
            create_decisions[decision["task_index"]] = decision

    fingerprints = set()
    for index, task in enumerate(intents["future_tasks"]):
        decision = create_decisions.get(index)
        group = future_groups.get(index)
        if not decision or not group:
            skipped.append(f"task {index}: no adjudication")
            continue
        candidate_ids = {c.get("id") for c in group["candidates"]}
        duplicate = decision.get("duplicate_candidate_id")
        if duplicate:
            duplicate_issue = next((c for c in group["candidates"] if c.get("id") == duplicate), None)
            duplicate_state = str(((duplicate_issue or {}).get("state") or {}).get("type") or "").lower()
            if duplicate in candidate_ids and duplicate_state not in _TERMINAL_TYPES:
                skipped.append(f"task {index}: existing issue {duplicate}")
            else:
                skipped.append(f"task {index}: invalid duplicate decision")
            continue
        # The second model is helpful for semantic adjudication, but it cannot
        # force a duplicate through when an open candidate has strong title
        # overlap with the proposed deliverable.
        obvious_duplicate = next((
            c for c in group["candidates"]
            if str((c.get("state") or {}).get("type") or "").lower() not in _TERMINAL_TYPES
            and title_overlap(task.get("title"), c.get("title")) >= 0.6
        ), None)
        if obvious_duplicate:
            skipped.append(f"task {index}: existing issue {obvious_duplicate.get('identifier') or obvious_duplicate.get('id')}")
            continue
        if not evidence_supported(task.get("evidence"), transcript_text):
            skipped.append(f"task {index}: evidence not verbatim")
            continue
        team = _exact_name(catalog["teams"], task.get("team_name"))
        user = _resolve_user(catalog, task.get("assignee_email"))
        if not team or not user or not _member_of(team, user):
            skipped.append(f"task {index}: unresolved team/assignee membership")
            continue
        project = _exact_name(catalog["projects"], task.get("project_name"))
        if task.get("project_name"):
            if not project or team.get("id") not in (project.get("team_ids") or []):
                skipped.append(f"task {index}: unresolved or cross-team project")
                continue
        priority = _priority(task.get("priority"))
        fingerprint = (_norm(task.get("title")), team.get("id"),
                       project.get("id") if project else None, user.get("id"))
        if fingerprint in fingerprints or not fingerprint[0]:
            skipped.append(f"task {index}: duplicate proposal")
            continue
        fingerprints.add(fingerprint)
        description = str(task.get("description") or "").strip()
        description += ("\n\nCreated from meeting: " + str(task.get("evidence")).strip())
        creations.append({
            "team_id": team["id"], "project_id": project.get("id") if project else None,
            "assignee_id": user["id"], "title": str(task.get("title")).strip()[:255],
            "description": description[:10000],
            "due_date": sensible_due_date(task.get("due_date"), priority, meeting_date),
            "priority": priority,
        })

    return {"complete": completions, "create": creations, "skipped": skipped}


def _completed_state(call, team_id):
    states = _paginate(call, LIST_STATES, "states", {"team_id": team_id, "first": 50})
    completed = [s for s in states if str(s.get("type") or "").lower() == "completed"]
    if not completed:
        completed = [s for s in states if _norm(s.get("name")) in {"done", "completed", "complete"}]
    if not completed:
        raise RuntimeError(f"no completed workflow state for team {team_id}")
    return completed[0]["id"]


def apply_plan(plan, call, dry=False):
    """Apply a validated plan exactly once, or return the dry-run preview."""
    results = {"completed": [], "created": [], "errors": [], "skipped": plan.get("skipped", [])}
    if dry:
        results["preview"] = {"complete": plan.get("complete", []), "create": plan.get("create", [])}
        return results

    state_cache = {}
    for action in plan.get("complete", []):
        try:
            current = _data(call(GET_ISSUE, {"issue_id": action["issue_id"]}))
            issue = current.get("issue") or current
            if str((issue.get("state") or {}).get("type") or "").lower() in _TERMINAL_TYPES:
                results["skipped"].append(f"{action['identifier']}: already terminal")
                continue
            team_id = (issue.get("team") or {}).get("id") or action.get("team_id")
            if not team_id:
                raise RuntimeError("issue has no team")
            if team_id not in state_cache:
                state_cache[team_id] = _completed_state(call, team_id)
            state_id = state_cache[team_id]
            response = call(UPDATE_ISSUE, {"issueId": action["issue_id"], "stateId": state_id})
            _data(response)
            results["completed"].append(action.get("identifier") or action["issue_id"])
        except Exception as exc:
            results["errors"].append(f"complete {action.get('identifier')}: {exc}")

    for action in plan.get("create", []):
        args = {key: value for key, value in action.items() if value is not None}
        try:
            payload = _data(call(CREATE_ISSUE, args))
            issue = payload.get("issue") or payload
            results["created"].append(issue.get("identifier") or issue.get("id") or action["title"])
        except Exception as exc:
            results["errors"].append(f"create {action.get('title')}: {exc}")
    return results


def reconcile(transcript, call, model, dry=False):
    plan = build_plan(transcript, call, model)
    return apply_plan(plan, call, dry=dry)


def summarize(result):
    completed = result.get("completed") or []
    created = result.get("created") or []
    preview = result.get("preview") or {}
    errors = result.get("errors") or []
    if preview:
        return f"Linear dry run: {len(preview.get('complete') or [])} complete, {len(preview.get('create') or [])} create"
    parts = [f"Linear: {len(completed)} completed, {len(created)} created"]
    if completed:
        parts.append("completed " + ", ".join(completed))
    if created:
        parts.append("created " + ", ".join(created))
    if errors:
        parts.append(f"{len(errors)} error(s): " + "; ".join(errors)[:500])
    return " | ".join(parts)
