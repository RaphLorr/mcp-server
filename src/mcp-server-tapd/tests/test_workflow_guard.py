import copy
import json
import unittest
from unittest.mock import Mock, patch

from mcp_server_tapd import server
from mcp_server_tapd.workflow_guard import (
    WorkflowValidationError,
    validate_status_change,
)


class WorkflowGuardTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.ticket = {
            "id": "1112345678000000001",
            "workspace_id": "12345678",
            "workitem_type_id": "type-a",
            "status": "status_3",
        }
        self.statuses = {
            "planning": "规划中",
            "status_3": "测试中",
            "resolved": "已实现",
            "rejected": "已拒绝",
        }
        self.client.get_stories.return_value = {
            "status": 1,
            "data": [{"Story": self.ticket}],
        }
        self.client.get_bug.return_value = {"status": 1, "data": [{"Bug": self.ticket}]}
        self.client.get_workflows_status_map.return_value = {
            "status": 1,
            "data": self.statuses,
        }
        self.client.get_workflows_last_steps.return_value = {
            "status": 1,
            "data": {"resolved": "已实现", "rejected": "已拒绝"},
        }
        self.client.get_workflows_all_transitions.return_value = {
            "status": 1,
            "data": [
                {"StepPrevious": "status_3", "StepNext": "resolved"},
                {"StepPrevious": "resolved", "StepNext": "end"},
            ],
        }
        self.payload = {
            "workspace_id": 12345678,
            "id": self.ticket["id"],
            "status": "resolved",
        }

    def test_end_pseudo_node_is_rejected_even_when_transitions_contain_it(self):
        self.payload["status"] = "end"
        with self.assertRaises(WorkflowValidationError) as error:
            validate_status_change(self.client, self.payload, "stories")
        result = json.loads(str(error.exception))
        self.assertFalse(result["updated"])
        self.assertNotIn("end", result["valid_statuses"])
        self.assertEqual(
            result["terminal_statuses"], {"resolved": "已实现", "rejected": "已拒绝"}
        )
        self.assertEqual(result["scope"]["workitem_type_id"], "type-a")

    def test_custom_alias_normalizes_to_the_current_categorys_key(self):
        self.payload.pop("status")
        self.payload["v_status"] = "已实现"
        validate_status_change(self.client, self.payload, "stories")
        self.assertEqual(self.payload["status"], "resolved")
        self.assertNotIn("v_status", self.payload)
        for name in (
            "get_workflows_status_map",
            "get_workflows_last_steps",
            "get_workflows_all_transitions",
        ):
            self.assertEqual(
                getattr(self.client, name).call_args.args[0],
                {
                    "workspace_id": 12345678,
                    "system": "story",
                    "workitem_type_id": "type-a",
                },
            )

    def test_conflicting_or_ambiguous_aliases_are_rejected(self):
        for alias in ("已拒绝", "不存在"):
            with self.subTest(alias=alias), self.assertRaises(WorkflowValidationError):
                validate_status_change(
                    self.client, {**self.payload, "v_status": alias}, "stories"
                )
        self.statuses["custom_resolved"] = "已实现"
        with self.assertRaises(WorkflowValidationError):
            validate_status_change(
                self.client, {**self.payload, "v_status": "已实现"}, "stories"
            )

    def test_unreachable_valid_state_does_not_skip_steps(self):
        self.payload["status"] = "planning"
        with self.assertRaises(WorkflowValidationError) as error:
            validate_status_change(self.client, self.payload, "stories")
        self.assertEqual(
            json.loads(str(error.exception))["next_statuses"], {"resolved": "已实现"}
        )

    def test_no_status_edit_does_not_query_workflows(self):
        validate_status_change(self.client, {"description": "updated"}, "stories")
        self.assertEqual(self.client.mock_calls, [])

    def test_tasks_do_not_use_story_workflow(self):
        validate_status_change(self.client, {**self.payload, "status": "done"}, "tasks")
        self.assertEqual(self.client.mock_calls, [])

    def test_short_ticket_id_still_requires_matching_project_and_unique_ticket(self):
        self.payload["id"] = "1"
        validate_status_change(self.client, self.payload, "stories")
        self.assertEqual(self.payload["status"], "resolved")

    def test_story_creation_with_explicit_state_requires_category(self):
        with self.assertRaises(WorkflowValidationError):
            validate_status_change(
                self.client, {"workspace_id": 12345678, "status": "resolved"}, "stories"
            )
        payload = {
            "workspace_id": 12345678,
            "status": "resolved",
            "workitem_type_id": "type-a",
        }
        validate_status_change(self.client, payload, "stories")
        self.client.get_stories.assert_not_called()

    def test_another_project_can_define_end_as_a_real_status(self):
        self.statuses["end"] = "归档"
        self.client.get_workflows_last_steps.return_value = {
            "status": 1,
            "data": {"end": "归档"},
        }
        self.ticket["status"] = "resolved"
        self.payload.update(workspace_id=98765432, status="end")
        self.ticket["workspace_id"] = "98765432"
        validate_status_change(self.client, self.payload, "stories")
        self.assertEqual(self.payload["status"], "end")
        self.assertEqual(
            self.client.get_workflows_status_map.call_args.args[0]["workspace_id"],
            98765432,
        )

    def test_bug_uses_its_own_project_workflow_without_story_category(self):
        self.client.get_workflows_status_map.return_value = {
            "status": 1,
            "data": {"open": "待处理", "custom_closed": "关闭"},
        }
        self.client.get_workflows_last_steps.return_value = {
            "status": 1,
            "data": {"custom_closed": "关闭"},
        }
        self.client.get_workflows_all_transitions.return_value = {
            "status": 1,
            "data": [{"StepPrevious": "open", "StepNext": "custom_closed"}],
        }
        self.ticket["status"] = "open"
        self.payload["status"] = "custom_closed"
        validate_status_change(self.client, self.payload, "bug")
        self.client.get_workflows_status_map.assert_called_once_with(
            {"workspace_id": 12345678, "system": "bug"}
        )
        self.assertNotIn(
            "workitem_type_id", self.client.get_bug.call_args.args[0]["fields"]
        )

    def test_query_errors_and_malformed_configuration_block_writes_without_secrets(
        self,
    ):
        for result in (
            {"status": 0, "info": "private-token"},
            {"status": 1, "data": {}},
            {"status": 1, "data": []},
        ):
            with self.subTest(result=result):
                self.client.get_workflows_status_map.return_value = result
                with self.assertRaises(WorkflowValidationError) as error:
                    validate_status_change(self.client, self.payload, "stories")
                self.assertNotIn("private-token", str(error.exception))
        self.client.get_workflows_status_map.side_effect = RuntimeError("private-token")
        with self.assertRaises(WorkflowValidationError) as error:
            validate_status_change(self.client, self.payload, "stories")
        self.assertNotIn("private-token", str(error.exception))

    def test_current_corrupt_state_is_reported_instead_of_inventing_a_repair_path(self):
        self.ticket["status"] = "end"
        with self.assertRaises(WorkflowValidationError) as error:
            validate_status_change(self.client, self.payload, "stories")
        self.assertEqual(json.loads(str(error.exception))["current_status"], "end")

    def test_missing_category_and_wrong_ticket_are_not_silently_accepted(self):
        for change in (
            {"workitem_type_id": None},
            {"workspace_id": "other"},
            {"id": "wrong"},
        ):
            original = copy.deepcopy(self.ticket)
            self.ticket.update(change)
            with (
                self.subTest(change=change),
                self.assertRaises(WorkflowValidationError),
            ):
                validate_status_change(self.client, self.payload, "stories")
            self.ticket.clear()
            self.ticket.update(original)

    def test_category_change_and_status_change_cannot_infer_cross_workflow_edges(self):
        with self.assertRaises(WorkflowValidationError):
            validate_status_change(
                self.client, {**self.payload, "workitem_type_id": "type-b"}, "stories"
            )

    def test_unchanged_status_needs_no_transition(self):
        self.payload["status"] = "status_3"
        validate_status_change(self.client, self.payload, "stories")
        self.client.get_workflows_all_transitions.assert_not_called()

    def test_invalid_state_is_blocked_at_all_mcp_write_entrypoints_before_uploads(self):
        for function, args in (
            (server.update_story_or_task, (12345678,)),
            (server.create_story_or_task, (12345678, "Story")),
            (server.update_bug, (12345678,)),
            (server.create_bug, (12345678, "Bug")),
        ):
            with (
                self.subTest(function=function.__name__),
                patch.object(server, "client", self.client),
                patch.object(server, "prepare_attachments") as upload,
            ):
                with self.assertRaises(WorkflowValidationError):
                    function(*args, options={"id": self.ticket["id"], "status": "end"})
                upload.assert_not_called()
        self.client.create_or_update_story.assert_not_called()
        self.client.create_or_update_bug.assert_not_called()

    def test_valid_alias_update_reaches_provider_once_with_canonical_state(self):
        self.client.create_or_update_story.return_value = {
            "status": 1,
            "data": {"Story": self.ticket},
        }
        options = {"id": self.ticket["id"], "v_status": "已实现"}
        with patch.object(server, "client", self.client):
            server.update_story_or_task(12345678, options)
        self.client.create_or_update_story.assert_called_once()
        sent = self.client.create_or_update_story.call_args.args[0]
        self.assertEqual(sent["status"], "resolved")
        self.assertNotIn("v_status", sent)
