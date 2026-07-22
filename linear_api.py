"""Small stdlib Linear GraphQL adapter used by the recap worker.

It exposes the same ``call(tool, arguments)`` shape used by ``linear_recap`` so
the safety and validation layer is independent from transport details.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import linear_recap


LINEAR_GQL = "https://api.linear.app/graphql"

_PAGE = "pageInfo { hasNextPage endCursor }"
_ISSUE_FIELDS = """
id identifier title url dueDate priority description
team { id name key }
project { id name }
assignee { id name displayName email }
state { id name type }
"""


class LinearAPI:
    def __init__(self, api_key=None, request=None):
        self.api_key = api_key or (
            os.environ.get("NEB_LINEAR_API_KEY")
            or os.environ.get("LINEAR_API_KEY")
            or os.environ.get("TMN_LINEAR_API_KEY")
        )
        self.request = request or self._request

    def __call__(self, tool, arguments):
        if not self.api_key:
            return {"_error": "NEB_LINEAR_API_KEY/LINEAR_API_KEY missing"}
        try:
            return {"successful": True, "data": self._dispatch(tool, dict(arguments or {})),
                    "error": None}
        except Exception as exc:
            return {"successful": False, "data": {}, "error": str(exc)}

    def _graphql(self, query, variables):
        response = self.request(query, variables)
        if not isinstance(response, dict):
            raise RuntimeError("Linear returned a non-object response")
        errors = response.get("errors") or []
        if errors:
            messages = "; ".join(str(e.get("message") or e) for e in errors if isinstance(e, dict))
            raise RuntimeError(messages or str(errors))
        data = response.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Linear response did not contain data")
        return data

    def _request(self, query, variables):
        body = json.dumps({"query": query, "variables": variables}).encode()
        req = urllib.request.Request(
            LINEAR_GQL, data=body,
            headers={"Authorization": self.api_key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:1000]
            raise RuntimeError(f"Linear HTTP {exc.code}: {detail}") from exc

    @staticmethod
    def _connection(connection, key):
        return {
            key: connection.get("nodes") or [],
            "page_info": connection.get("pageInfo") or {},
        }

    def _dispatch(self, tool, args):
        if tool == linear_recap.LIST_TEAMS:
            query = f"""query Teams($first:Int,$after:String) {{
              teams(first:$first,after:$after) {{ nodes {{
                id name key
                members(first:50) {{ nodes {{ id name email }} }}
                projects(first:50) {{ nodes {{ id name }} }}
              }} {_PAGE} }}
            }}"""
            # Nested memberships/projects multiply GraphQL complexity, so cap
            # the outer page and rely on normal cursor pagination.
            data = self._graphql(query, {"first": min(args.get("first", 25), 25),
                                         "after": args.get("after")})
            result = self._connection(data["teams"], "teams")
            for team in result["teams"]:
                team["members"] = (team.get("members") or {}).get("nodes") or []
                team["projects"] = (team.get("projects") or {}).get("nodes") or []
            return result

        if tool == linear_recap.LIST_PROJECTS:
            query = f"""query Projects($first:Int,$after:String) {{
              projects(first:$first,after:$after,includeArchived:false) {{
                nodes {{ id name }} {_PAGE}
              }}
            }}"""
            data = self._graphql(query, {"first": args.get("first", 50), "after": args.get("after")})
            return self._connection(data["projects"], "projects")

        if tool == linear_recap.LIST_USERS:
            query = f"""query Users($first:Int,$after:String) {{
              users(first:$first,after:$after,includeDisabled:false) {{
                nodes {{ id name displayName email active admin }} {_PAGE}
              }}
            }}"""
            data = self._graphql(query, {"first": args.get("first", 50), "after": args.get("after")})
            return self._connection(data["users"], "users")

        if tool == linear_recap.SEARCH_ISSUES:
            query = f"""query Search($query:String!,$first:Int,$after:String,$includeArchived:Boolean) {{
              searchIssues(term:$query,first:$first,after:$after,includeArchived:$includeArchived) {{
                nodes {{ {_ISSUE_FIELDS} }} {_PAGE}
              }}
            }}"""
            data = self._graphql(query, {
                "query": args.get("query"), "first": args.get("first", 15),
                "after": args.get("after"), "includeArchived": args.get("include_archived", False),
            })
            return self._connection(data["searchIssues"], "issues")

        if tool == linear_recap.LIST_TEAM_ISSUES:
            query = f"""query TeamIssues($teamId:ID!,$first:Int,$after:String,$excludedStateTypes:[String!]) {{
              issues(first:$first,after:$after,orderBy:updatedAt,
                     filter:{{team:{{id:{{eq:$teamId}}}},state:{{type:{{nin:$excludedStateTypes}}}}}}) {{
                nodes {{ {_ISSUE_FIELDS} }} {_PAGE}
              }}
            }}"""
            data = self._graphql(query, {
                "teamId": args["team_id"], "first": args.get("first", 100),
                "after": args.get("after"),
                "excludedStateTypes": ["completed", "canceled"],
            })
            return self._connection(data["issues"], "issues")

        if tool == linear_recap.GET_ISSUE:
            query = f"""query Issue($id:String!) {{ issue(id:$id) {{ {_ISSUE_FIELDS} }} }}"""
            data = self._graphql(query, {"id": args["issue_id"]})
            return {"issue": data["issue"]}

        if tool == linear_recap.LIST_STATES:
            query = f"""query States($teamId:ID!,$first:Int,$after:String) {{
              workflowStates(first:$first,after:$after,filter:{{team:{{id:{{eq:$teamId}}}}}}) {{
                nodes {{ id name type }} {_PAGE}
              }}
            }}"""
            data = self._graphql(query, {
                "teamId": args["team_id"], "first": args.get("first", 50),
                "after": args.get("after"),
            })
            return self._connection(data["workflowStates"], "states")

        if tool == linear_recap.UPDATE_ISSUE:
            query = f"""mutation UpdateIssue($id:String!,$input:IssueUpdateInput!) {{
              issueUpdate(id:$id,input:$input) {{ success issue {{ {_ISSUE_FIELDS} }} }}
            }}"""
            update = {}
            mapping = {
                "stateId": "stateId", "dueDate": "dueDate", "assigneeId": "assigneeId",
                "projectId": "projectId", "priority": "priority", "description": "description",
                "title": "title", "teamId": "teamId",
            }
            for source, target in mapping.items():
                if source in args:
                    update[target] = args[source]
            data = self._graphql(query, {"id": args["issueId"], "input": update})
            if not data["issueUpdate"].get("success"):
                raise RuntimeError("Linear issue update was not successful")
            return {"issue": data["issueUpdate"].get("issue")}

        if tool == linear_recap.CREATE_ISSUE:
            query = f"""mutation CreateIssue($input:IssueCreateInput!) {{
              issueCreate(input:$input) {{ success issue {{ {_ISSUE_FIELDS} }} }}
            }}"""
            mapping = {
                "team_id": "teamId", "title": "title", "description": "description",
                "project_id": "projectId", "assignee_id": "assigneeId",
                "due_date": "dueDate", "priority": "priority",
            }
            create = {target: args[source] for source, target in mapping.items()
                      if source in args and args[source] is not None}
            data = self._graphql(query, {"input": create})
            if not data["issueCreate"].get("success"):
                raise RuntimeError("Linear issue create was not successful")
            return {"issue": data["issueCreate"].get("issue")}

        raise ValueError(f"unsupported Linear operation: {tool}")
