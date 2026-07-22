#!/usr/bin/env python3
from datetime import date
import unittest

import linear_recap


TEAM_ID = "11111111-1111-1111-1111-111111111111"
OTHER_TEAM_ID = "22222222-2222-2222-2222-222222222222"
PROJECT_ID = "33333333-3333-3333-3333-333333333333"
USER_ID = "44444444-4444-4444-4444-444444444444"
ISSUE_ID = "55555555-5555-5555-5555-555555555555"


def catalog(project_team=TEAM_ID):
    return {
        "teams": [{
            "id": TEAM_ID, "name": "Team Nebula",
            "members": [{"id": USER_ID, "name": "Shawn Reddy", "email": "shawn@teamnebula.ai"}],
            "projects": [{"id": PROJECT_ID}],
        }],
        "projects": [{"id": PROJECT_ID, "name": "Internal Meeting Scheduler",
                      "team_ids": [project_team]}],
        "users": [{"id": USER_ID, "name": "Shawn Reddy", "displayName": "screddy",
                   "email": "shawn@teamnebula.ai", "active": True}],
    }


def candidate(state_type="started"):
    return {
        "id": ISSUE_ID, "identifier": "TMN-123", "title": "Deploy meeting scheduler webhook",
        "team": {"id": TEAM_ID, "name": "Team Nebula"},
        "project": {"id": PROJECT_ID, "name": "Internal Meeting Scheduler"},
        "state": {"id": "state", "name": "In Progress", "type": state_type},
    }


def completion_inputs(candidate_id=ISSUE_ID, evidence="I deployed the meeting scheduler webhook yesterday."):
    intents = {
        "completion_claims": [{
            "completed_work": "Deploy meeting scheduler webhook",
            "search_query": "deploy meeting scheduler webhook",
            "evidence": evidence,
        }],
        "future_tasks": [],
    }
    decisions = {"complete": [{
        "claim_index": 0, "candidate_id": candidate_id, "confidence": "high",
        "reason": "Exact deliverable",
    }], "create": []}
    groups = [{"kind": "completion", "intent_index": 0,
               "intent": intents["completion_claims"][0], "candidates": [candidate()]}]
    return intents, decisions, groups


class TestCompletionValidation(unittest.TestCase):
    def test_exact_candidate_with_verbatim_evidence_is_allowed(self):
        intents, decisions, groups = completion_inputs()
        plan = linear_recap.validate_plan(
            intents, decisions, groups, catalog(),
            "Shawn: I deployed the meeting scheduler webhook yesterday.", date(2026, 7, 20))
        self.assertEqual([a["identifier"] for a in plan["complete"]], ["TMN-123"])

    def test_model_cannot_invent_issue_id(self):
        intents, decisions, groups = completion_inputs(candidate_id="invented")
        plan = linear_recap.validate_plan(
            intents, decisions, groups, catalog(),
            "I deployed the meeting scheduler webhook yesterday.", date(2026, 7, 20))
        self.assertEqual(plan["complete"], [])

    def test_paraphrased_evidence_is_rejected(self):
        intents, decisions, groups = completion_inputs(evidence="The webhook is done.")
        plan = linear_recap.validate_plan(
            intents, decisions, groups, catalog(),
            "I deployed the meeting scheduler webhook yesterday.", date(2026, 7, 20))
        self.assertEqual(plan["complete"], [])

    def test_already_completed_issue_is_rejected(self):
        intents, decisions, groups = completion_inputs()
        groups[0]["candidates"] = [candidate(state_type="completed")]
        plan = linear_recap.validate_plan(
            intents, decisions, groups, catalog(),
            "I deployed the meeting scheduler webhook yesterday.", date(2026, 7, 20))
        self.assertEqual(plan["complete"], [])


class TestCreationValidation(unittest.TestCase):
    def _inputs(self, **overrides):
        task = {
            "title": "Send final scheduler launch plan",
            "description": "Share the approved launch sequence with the team.",
            "team_name": "Team Nebula",
            "project_name": "Internal Meeting Scheduler",
            "assignee_email": "shawn@teamnebula.ai",
            "due_date": "2026-07-25",
            "priority": "high",
            "evidence": "I will send the final scheduler launch plan by Friday.",
        }
        task.update(overrides)
        intents = {"completion_claims": [], "future_tasks": [task]}
        groups = [{"kind": "future", "intent_index": 0, "intent": task, "candidates": []}]
        decisions = {"complete": [], "create": [{
            "task_index": 0, "duplicate_candidate_id": "", "reason": "No match",
        }]}
        return intents, decisions, groups

    def test_resolves_team_project_assignee_and_due_date(self):
        inputs = self._inputs()
        plan = linear_recap.validate_plan(
            *inputs, catalog(),
            "I will send the final scheduler launch plan by Friday.", date(2026, 7, 20))
        action = plan["create"][0]
        self.assertEqual(action["team_id"], TEAM_ID)
        self.assertEqual(action["project_id"], PROJECT_ID)
        self.assertEqual(action["assignee_id"], USER_ID)
        self.assertEqual(action["due_date"], "2026-07-25")
        self.assertEqual(action["priority"], 2)

    def test_invalid_date_gets_priority_based_fallback(self):
        inputs = self._inputs(due_date="someday", priority="urgent")
        plan = linear_recap.validate_plan(
            *inputs, catalog(),
            "I will send the final scheduler launch plan by Friday.", date(2026, 7, 20))
        self.assertEqual(plan["create"][0]["due_date"], "2026-07-22")

    def test_unknown_priority_falls_back_to_normal_due_date(self):
        self.assertEqual(
            linear_recap.sensible_due_date("", 99, date(2026, 7, 20)),
            "2026-08-03",
        )

    def test_cross_team_project_is_rejected(self):
        inputs = self._inputs()
        plan = linear_recap.validate_plan(
            *inputs, catalog(project_team=OTHER_TEAM_ID),
            "I will send the final scheduler launch plan by Friday.", date(2026, 7, 20))
        self.assertEqual(plan["create"], [])

    def test_existing_candidate_suppresses_creation(self):
        intents, decisions, groups = self._inputs()
        groups[0]["candidates"] = [candidate()]
        decisions["create"][0]["duplicate_candidate_id"] = ISSUE_ID
        plan = linear_recap.validate_plan(
            intents, decisions, groups, catalog(),
            "I will send the final scheduler launch plan by Friday.", date(2026, 7, 20))
        self.assertEqual(plan["create"], [])
        self.assertIn(ISSUE_ID, plan["skipped"][0])

    def test_obvious_open_duplicate_is_suppressed_even_if_model_misses_it(self):
        intents, decisions, groups = self._inputs(title="Deploy meeting scheduler webhook")
        groups[0]["candidates"] = [candidate()]
        plan = linear_recap.validate_plan(
            intents, decisions, groups, catalog(),
            "I will send the final scheduler launch plan by Friday.", date(2026, 7, 20))
        self.assertEqual(plan["create"], [])
        self.assertIn("TMN-123", plan["skipped"][0])


class TestApplyPlan(unittest.TestCase):
    def test_dry_run_never_calls_linear(self):
        calls = []
        result = linear_recap.apply_plan(
            {"complete": [{"issue_id": ISSUE_ID}], "create": [{"title": "Task"}], "skipped": []},
            lambda tool, args: calls.append((tool, args)), dry=True)
        self.assertEqual(calls, [])
        self.assertEqual(len(result["preview"]["complete"]), 1)
        self.assertEqual(len(result["preview"]["create"]), 1)

    def test_refetches_issue_then_updates_and_creates(self):
        calls = []

        def fake_call(tool, args):
            calls.append((tool, args))
            if tool == linear_recap.GET_ISSUE:
                return {"data": {"issue": candidate()}}
            if tool == linear_recap.LIST_STATES:
                return {"data": {"states": [{"id": "done-state", "name": "Done", "type": "completed"}],
                                             "page_info": {"hasNextPage": False}}}
            if tool == linear_recap.UPDATE_ISSUE:
                return {"successful": True, "data": {"issue": {"identifier": "TMN-123"}}}
            if tool == linear_recap.CREATE_ISSUE:
                return {"successful": True, "data": {"issue": {"identifier": "TMN-124"}}}
            raise AssertionError(tool)

        plan = {
            "complete": [{"issue_id": ISSUE_ID, "identifier": "TMN-123",
                          "team_id": TEAM_ID, "title": "Deploy meeting scheduler webhook"}],
            "create": [{"team_id": TEAM_ID, "title": "Send launch plan",
                        "description": "Details", "assignee_id": USER_ID,
                        "due_date": "2026-07-25", "priority": 2, "project_id": PROJECT_ID}],
            "skipped": [],
        }
        result = linear_recap.apply_plan(plan, fake_call)
        self.assertEqual(result["completed"], ["TMN-123"])
        self.assertEqual(result["created"], ["TMN-124"])
        self.assertEqual([tool for tool, _ in calls], [
            linear_recap.GET_ISSUE, linear_recap.LIST_STATES,
            linear_recap.UPDATE_ISSUE, linear_recap.CREATE_ISSUE,
        ])


if __name__ == "__main__":
    unittest.main()
