#!/usr/bin/env python3
import unittest

import linear_api
import linear_recap


class RecordingRequest:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, query, variables):
        self.calls.append((query, variables))
        return self.responses.pop(0)


class TestLinearAPI(unittest.TestCase):
    def test_team_connection_is_normalized_for_catalog(self):
        request = RecordingRequest([{"data": {"teams": {
            "nodes": [{
                "id": "team", "name": "Team Nebula", "key": "TMN",
                "members": {"nodes": [{"id": "user", "name": "Shawn", "email": "s@tmn.ai"}]},
                "projects": {"nodes": [{"id": "project", "name": "Scheduler"}]},
            }],
            "pageInfo": {"hasNextPage": False, "endCursor": "team"},
        }}}])
        client = linear_api.LinearAPI(api_key="test", request=request)
        response = client(linear_recap.LIST_TEAMS, {"first": 50})
        team = response["data"]["teams"][0]
        self.assertEqual(team["members"][0]["id"], "user")
        self.assertEqual(team["projects"][0]["id"], "project")
        self.assertEqual(response["data"]["page_info"]["endCursor"], "team")

    def test_search_maps_snake_case_archive_flag(self):
        request = RecordingRequest([{"data": {"searchIssues": {
            "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None},
        }}}])
        client = linear_api.LinearAPI(api_key="test", request=request)
        response = client(linear_recap.SEARCH_ISSUES, {
            "query": "scheduler", "first": 15, "include_archived": False,
        })
        self.assertTrue(response["successful"])
        self.assertEqual(request.calls[0][1]["includeArchived"], False)
        self.assertEqual(response["data"]["issues"], [])

    def test_create_maps_validated_fields_to_graphql_input(self):
        request = RecordingRequest([{"data": {"issueCreate": {
            "success": True, "issue": {"id": "issue", "identifier": "TMN-1"},
        }}}])
        client = linear_api.LinearAPI(api_key="test", request=request)
        response = client(linear_recap.CREATE_ISSUE, {
            "team_id": "team", "project_id": "project", "assignee_id": "user",
            "title": "Send plan", "description": "Details", "due_date": "2026-07-25",
            "priority": 2,
        })
        graphql_input = request.calls[0][1]["input"]
        self.assertEqual(graphql_input, {
            "teamId": "team", "projectId": "project", "assigneeId": "user",
            "title": "Send plan", "description": "Details", "dueDate": "2026-07-25",
            "priority": 2,
        })
        self.assertEqual(response["data"]["issue"]["identifier"], "TMN-1")

    def test_team_issue_listing_requests_open_issues_for_the_team(self):
        request = RecordingRequest([{"data": {"issues": {
            "nodes": [{"id": "issue", "identifier": "TMN-9", "title": "Draft Acme SOW"}],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }}}])
        client = linear_api.LinearAPI(api_key="test", request=request)
        response = client(linear_recap.LIST_TEAM_ISSUES, {"team_id": "team", "first": 100})
        variables = request.calls[0][1]
        self.assertEqual(variables["teamId"], "team")
        self.assertEqual(variables["excludedStateTypes"], ["completed", "canceled"])
        self.assertEqual(response["data"]["issues"][0]["identifier"], "TMN-9")

    def test_graphql_errors_are_returned_without_raising_to_caller(self):
        request = RecordingRequest([{"errors": [{"message": "denied"}]}])
        client = linear_api.LinearAPI(api_key="test", request=request)
        response = client(linear_recap.LIST_USERS, {"first": 50})
        self.assertFalse(response["successful"])
        self.assertEqual(response["error"], "denied")


if __name__ == "__main__":
    unittest.main()
