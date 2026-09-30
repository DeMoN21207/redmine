import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from mcp import Client as MCPClient

import server
from redmine_client import Client, NoRedirect, RedmineError, issue_id


class Response(io.BytesIO):
    def __init__(self, value=None, status=200, raw=None, content_type="application/json"):
        super().__init__(raw if raw is not None else json.dumps(value or {}).encode("utf-8"))
        self.status = status
        self.headers = {"Content-Type": content_type}


def card():
    return {"id": 42, "subject": "Initial", "description": "Description", "project": {"id": 3},
            "status": {"id": 1}, "updated_on": "2026-09-30T10:00:00Z",
            "allowed_statuses": [{"id": 1}, {"id": 2}], "journals": []}


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.client = Client("https://redmine.example", "test-secret", 5)
        self.settings = patch.object(server, "load_settings", return_value=(self.client.base, self.client.key, None))
        self.factory = patch.object(server, "Client", return_value=self.client)
        self.environment = patch.dict(os.environ, {"REDMINE_READ_ONLY": "false"})
        for context in (self.settings, self.factory, self.environment):
            context.start()
            self.addCleanup(context.stop)

    def transport(self, *responses):
        return patch.object(self.client.opener, "open", side_effect=responses)

    def test_create_serializes_post_and_verifies_get(self):
        created = card()
        created.update(subject="Новая задача", description="Текст")
        with self.transport(Response({"issue": {"id": 42}}, status=201), Response({"issue": created})) as opened:
            result = server.redmine_create_issue("Новая задача", "3", "Текст")
        requests = [call.args[0] for call in opened.call_args_list]
        self.assertEqual([request.get_method() for request in requests], ["POST", "GET"])
        self.assertEqual(json.loads(requests[0].data), {"issue": {"subject": "Новая задача", "description": "Текст", "project_id": 3}})
        self.assertNotIn("test-secret", requests[0].full_url)
        self.assertTrue(result["data"]["verified"])

    def test_update_minimal_put_then_get(self):
        after = card()
        after["subject"] = "Updated"
        with self.transport(Response({"issue": card()}), Response(status=204, raw=b""), Response({"issue": after})) as opened:
            result = server.redmine_update_issue("#42", {"subject": "Updated"})
        requests = [call.args[0] for call in opened.call_args_list]
        self.assertEqual([request.get_method() for request in requests], ["GET", "PUT", "GET"])
        self.assertEqual(json.loads(requests[1].data), {"issue": {"subject": "Updated"}})
        self.assertTrue(result["data"]["verified"])

    def test_change_status_and_comment_are_verified(self):
        after = card()
        after.update(status={"id": 2}, journals=[{"id": 7, "notes": "Done", "private_notes": False}])
        with self.transport(Response({"issue": card()}), Response(status=204, raw=b""), Response({"issue": after})) as opened:
            result = server.redmine_change_status("42", 2, "Done")
        self.assertEqual(json.loads(opened.call_args_list[1].args[0].data), {"issue": {"status_id": 2, "notes": "Done"}})
        self.assertEqual(result["data"]["journal_id"], 7)
        self.assertTrue(result["data"]["verified"])

    def test_private_comment_and_new_journal(self):
        before, after = card(), card()
        before["journals"] = [{"id": 1, "notes": "Note", "private_notes": True}]
        after["journals"] = before["journals"] + [{"id": 2, "notes": "Note", "private_notes": True}]
        with self.transport(Response({"issue": before}), Response(status=204, raw=b""), Response({"issue": after})):
            result = server.redmine_add_comment("42", "Note", private=True)
        self.assertEqual(result["data"]["journal_id"], 2)

    def test_existing_comment_does_not_count_as_success(self):
        before = card()
        before["journals"] = [{"id": 1, "notes": "Note"}]
        with self.transport(Response({"issue": before}), Response(status=204, raw=b""), Response({"issue": before})):
            with self.assertRaisesRegex(server.ToolError, "new comment was not confirmed"):
                server.redmine_add_comment("42", "Note")

    def test_stale_issue_blocks_put(self):
        with self.transport(Response({"issue": card()})) as opened:
            with self.assertRaisesRegex(server.ToolError, "changed since"):
                server.redmine_update_issue("42", {"subject": "Updated"}, expected_updated_on="old")
        self.assertEqual(opened.call_count, 1)

    def test_invalid_status_blocks_put(self):
        with self.transport(Response({"issue": card()})) as opened:
            with self.assertRaisesRegex(server.ToolError, "transition is not allowed"):
                server.redmine_change_status("42", 9)
        self.assertEqual(opened.call_count, 1)

    def test_unconfirmed_field_is_not_success(self):
        with self.transport(Response({"issue": card()}), Response(status=204, raw=b""), Response({"issue": card()})):
            with self.assertRaisesRegex(server.ToolError, "values were not confirmed"):
                server.redmine_update_issue("42", {"subject": "Updated"})

    def test_verification_failure_is_reported_as_accepted_write(self):
        with self.transport(Response({"issue": card()}), Response(status=204, raw=b""), URLError("unreachable")):
            with self.assertRaisesRegex(server.ToolError, "Write accepted.*verification failed"):
                server.redmine_update_issue("42", {"subject": "Updated"})

    def test_read_only_mode_blocks_all_writes_without_http(self):
        with patch.dict(os.environ, {"REDMINE_READ_ONLY": "true"}), self.transport() as opened:
            for operation in (lambda: server.redmine_create_issue("New", "3"),
                              lambda: server.redmine_update_issue("42", {"subject": "New"}),
                              lambda: server.redmine_change_status("42", 2),
                              lambda: server.redmine_add_comment("42", "Note")):
                with self.assertRaisesRegex(server.ToolError, "Writes are disabled"):
                    operation()
        opened.assert_not_called()

    def test_invalid_fields_are_rejected_before_http(self):
        bad = [{"unknown": 1}, {"subject": " "}, {"status_id": "2"}, {"done_ratio": True},
               {"due_date": "2026-02-30"}, {"custom_fields": [{"id": 1, "value": 3}]},
               {"estimated_hours": float("nan")}, {"watcher_user_ids": ["7"]}]
        with self.transport() as opened:
            for fields in bad:
                with self.assertRaises(server.ToolError):
                    server.redmine_update_issue("42", fields)
        opened.assert_not_called()

    def test_project_identifier_is_resolved_for_issue_list(self):
        with self.transport(Response({"project": {"id": 3}}), Response({"issues": [], "total_count": 0})) as opened:
            server.redmine_list_issues(project="example", limit=1)
        self.assertIn("project_id=3", opened.call_args.args[0].full_url)

    def test_history_truncation_and_pagination_are_explicit(self):
        value = card()
        value["journals"] = [{"id": item, "created_on": f"2026-09-30T10:00:0{item}Z"} for item in (3, 1, 2)]
        with self.transport(Response({"issue": value})):
            recent = server.redmine_get_issue("42", history_limit=1)["data"]
        self.assertEqual(recent["history_total"], 3)
        self.assertTrue(recent["history_truncated"])
        self.assertEqual(recent["issue"]["journals"][0]["id"], 3)
        with self.transport(Response({"issue": value})):
            page = server.redmine_get_issue_history("42", offset=1, limit=1)["data"]
        self.assertEqual(page["journals"][0]["id"], 2)
        self.assertEqual(page["total_count"], 3)

    def test_download_uses_cache_and_sanitized_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"REDMINE_CACHE_DIR": directory}), self.transport(Response({"attachment": {"filename": "../../bad.exe"}})), patch.object(server, "attachment", return_value={"saved": True}) as downloaded:
                server.redmine_download_attachment(7)
            target = downloaded.call_args.args[2]
            self.assertEqual(target.parent, Path(directory).resolve())
            self.assertNotIn("/", target.name)

    def test_custom_fields_and_null_clearing_verification(self):
        value = card()
        value.update(custom_fields=[{"id": 1, "value": ["a", "b"]}], due_date=None)
        server.verify_fields(value, {"custom_fields": [{"id": 1, "value": ["a", "b"]}], "due_date": None, "assigned_to_id": None})


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.client = Client("https://redmine.example", "test-secret", 5)

    def test_get_auth_header_and_unicode_query(self):
        with patch.object(self.client.opener, "open", return_value=Response({"issues": []})) as opened:
            self.client.get("issues.json", {"q": "платёж"})
        request = opened.call_args.args[0]
        self.assertEqual(request.get_header("X-redmine-api-key"), "test-secret")
        self.assertIn("%D0", request.full_url)
        self.assertNotIn("test-secret", request.full_url)

    def test_post_utf8_json_content_type(self):
        with patch.object(self.client.opener, "open", return_value=Response({"issue": {"id": 1}}, status=201)) as opened:
            self.client.write("POST", "issues.json", {"issue": {"subject": "Привет"}})
        request = opened.call_args.args[0]
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data)["issue"]["subject"], "Привет")

    def test_delete_and_arbitrary_mutation_routes_rejected(self):
        for method, path in (("DELETE", "issues/1.json"), ("POST", "projects.json"), ("PUT", "https://evil.example/issues/1.json")):
            with self.assertRaises(RedmineError):
                self.client.write(method, path, {})

    def test_422_validation_message_redacts_secret(self):
        error = HTTPError("https://redmine.example", 422, "", {}, io.BytesIO(b'{"errors":["Invalid field test-secret"]}'))
        with patch.object(self.client.opener, "open", side_effect=error):
            with self.assertRaises(RedmineError) as captured:
                self.client.write("POST", "issues.json", {})
        self.assertIn("Invalid field [REDACTED]", str(captured.exception))
        self.assertNotIn("test-secret", str(captured.exception))

    def test_network_write_outcome_is_unknown_and_not_retried(self):
        with patch.object(self.client.opener, "open", side_effect=URLError("timeout test-secret")) as opened:
            with self.assertRaisesRegex(RedmineError, "Write outcome unknown") as captured:
                self.client.write("POST", "issues.json", {})
        self.assertNotIn("test-secret", str(captured.exception))
        opened.assert_called_once()

    def test_cross_host_http_and_redirect_rejected(self):
        with patch.object(self.client.opener, "open") as opened:
            for url in ("https://evil.example/file", "http://redmine.example/file"):
                with self.assertRaises(RedmineError):
                    self.client.open_url(url)
            with self.assertRaises(RedmineError):
                issue_id("https://evil.example/issues/1", self.client.base)
        opened.assert_not_called()
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example"))

    def test_html_login_is_not_valid_api_response(self):
        with patch.object(self.client.opener, "open", return_value=Response(raw=b"<html>login</html>", content_type="text/html")):
            with self.assertRaisesRegex(RedmineError, "Expected JSON"):
                self.client.get("issues/1.json")


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_structured_response_and_invalid_write(self):
        client = Client("https://redmine.example", "test-secret", 5)
        with patch.object(server, "load_settings", return_value=(client.base, client.key, None)), patch.object(server, "Client", return_value=client), patch.object(client.opener, "open", return_value=Response({"user": {"id": 7, "login": "user", "api_key": "test-secret"}})) as opened:
            async with MCPClient(server.server) as connection:
                tools = (await connection.list_tools()).tools
                self.assertEqual(len(tools), 14)
                names = {tool.name: tool for tool in tools}
                self.assertTrue(names["redmine_get_issue"].annotations.read_only_hint)
                self.assertFalse(names["redmine_create_issue"].annotations.read_only_hint)
                result = await connection.call_tool("redmine_current_user", {})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content["data"]["user"], {"id": 7, "login": "user"})
                invalid = await connection.call_tool("redmine_update_issue", {"issue": "42", "fields": {"unknown": 1}})
                self.assertTrue(invalid.is_error)
        opened.assert_called_once()


if __name__ == "__main__":
    unittest.main()
