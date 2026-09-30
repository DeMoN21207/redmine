"""Verify stdio tool discovery and real read-only Redmine requests."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from mcp import Client, StdioServerParameters


async def check(args):
    params = StdioServerParameters(command=args.python,
                                  args=["-X", "utf8", str(Path(args.server).resolve())],
                                  env=dict(os.environ), cwd=str(Path(args.server).resolve().parent))
    async with Client(params, read_timeout_seconds=60, mode=args.mode) as client:
        tools = (await client.list_tools()).tools
        names = {tool.name for tool in tools}
        required = {"redmine_current_user", "redmine_get_issue", "redmine_get_issue_history",
                    "redmine_list_issues", "redmine_search_issues", "redmine_list_projects",
                    "redmine_get_attachment", "redmine_download_attachment", "redmine_get_metadata",
                    "redmine_get_allowed_statuses", "redmine_create_issue", "redmine_update_issue",
                    "redmine_change_status", "redmine_add_comment"}
        if names != required:
            raise RuntimeError(f"Unexpected tools: {sorted(names)}")
        for tool in tools:
            properties = tool.input_schema.get("properties", {})
            if any(key in properties for key in ("api_key", "token", "config", "redmine_url")):
                raise RuntimeError("Credentials must not be model-visible tool arguments.")
        result = await client.call_tool("redmine_current_user", {})
        if result.is_error:
            raise RuntimeError("Authentication tool returned an error: " + str(result.content))
        data = result.structured_content["data"]
        report = {"server": client.server_info.name, "tool_count": len(tools),
                  "authenticated": True, "user_id": data["user"]["id"], "write_tools_discovered": True}
        if args.issue:
            result = await client.call_tool("redmine_get_issue", {"issue": args.issue})
            if result.is_error:
                raise RuntimeError("Issue tool returned an error: " + str(result.content))
            card = result.structured_content["data"]
            report["issue_id"] = card["issue"]["id"]
            report["history_total"] = card["history_total"]
            history = await client.call_tool("redmine_get_issue_history", {"issue": args.issue, "limit": 2})
            if history.is_error:
                raise RuntimeError("History tool returned an error.")
            report["history_page_size"] = len(history.structured_content["data"]["journals"])
            allowed = await client.call_tool("redmine_get_allowed_statuses", {"issue": args.issue})
            if allowed.is_error:
                raise RuntimeError("Allowed status tool returned an error.")
            report["allowed_statuses_supported"] = allowed.structured_content["data"]["supported"]
            project = str(card["issue"]["project"]["id"])
            for tool, arguments in (
                ("redmine_get_metadata", {"resource": "project", "project": project}),
                ("redmine_list_issues", {"project": project, "limit": 1}),
                ("redmine_list_projects", {"limit": 1}),
                ("redmine_search_issues", {"query": card["issue"]["subject"].split()[0], "project": project, "limit": 1}),
            ):
                checked = await client.call_tool(tool, arguments)
                if checked.is_error:
                    raise RuntimeError(f"Read tool {tool} returned an error: {checked.content}")
            report["read_tools_verified"] = True
        # Exercise validation through stdio without any mutation or API request.
        invalid = await client.call_tool("redmine_update_issue", {"issue": "1", "fields": {"invalid_field": 1}})
        if not invalid.is_error:
            raise RuntimeError("Invalid mutation fields were not rejected.")
        report["invalid_write_rejected"] = True
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True)
    parser.add_argument("--server", default=str(Path(__file__).with_name("server.py")))
    parser.add_argument("--issue")
    parser.add_argument("--mode", choices=("legacy", "auto"), default="legacy")
    print(json.dumps(asyncio.run(check(parser.parse_args())), ensure_ascii=False))
