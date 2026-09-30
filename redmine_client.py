#!/usr/bin/env python3
"""Redmine HTTPS client. Credentials are loaded locally and passed only as a header."""

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_URL = "https://redmine.redermio.ru"
RESOURCES = {
    "issues", "projects", "search", "users", "attachments", "issue_statuses",
    "trackers", "enumerations", "queries", "versions", "issue_categories",
    "roles", "groups", "custom_fields", "time_entries", "memberships", "news",
}
SECRET_PARAMS = {"key", "api_key", "password", "token", "access_token"}


class RedmineError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_config(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        # JSONDecodeError must not expose the document containing the key.
        raise RedmineError(f"Cannot read valid JSON configuration: {path}") from exc
    if not isinstance(value, dict):
        raise RedmineError(f"Configuration must be a JSON object: {path}")
    return value


def load_settings(explicit_path, include_project_config=True):
    chosen = explicit_path or os.environ.get("REDMINE_CONFIG")
    if chosen:
        config = read_config(Path(chosen).expanduser())
    else:
        paths = [Path.home() / ".codex/redmine-config.json",
                 Path.home() / ".claude/redmine-config.json"]
        found = next((path for path in paths if path.is_file()), None)
        config = read_config(found) if found else {}
    base = os.environ.get("REDMINE_URL") or config.get("redmine_url") or DEFAULT_URL
    key = os.environ.get("REDMINE_API_KEY") or config.get("api_key")
    if not isinstance(base, str) or not isinstance(key, str) or not key.strip():
        raise RedmineError("Set api_key in local configuration or REDMINE_API_KEY.")
    if any(char in key for char in "\r\n"):
        raise RedmineError("The API key contains an invalid newline.")
    parsed = urlparse(base)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise RedmineError("redmine_url must be an HTTPS base URL without credentials or query.")
    project = config.get("default_project_id")
    project_path = Path.cwd() / ".claude/redmine.json"
    if include_project_config and project_path.is_file():
        project = read_config(project_path).get("project_id") or project
    return base.rstrip("/"), key.strip(), project


def origin(url):
    parts = urlparse(url)
    try:
        return parts.scheme, parts.hostname, parts.port or 443
    except ValueError as exc:
        raise RedmineError("Invalid URL port.") from exc


class Client:
    def __init__(self, base, key, timeout):
        self.base, self.key, self.timeout = base, key, timeout
        self.opener = build_opener(NoRedirect())
        self.last_url = None

    def redact(self, value):
        return str(value).replace(self.key, "[REDACTED]")

    def open_url(self, url, accept="application/json"):
        parsed = urlparse(url)
        if (origin(url) != origin(self.base) or parsed.username or parsed.password
                or parsed.fragment or parsed.query):
            # API queries are constructed by get(); attachment URLs cannot inject queries.
            raise RedmineError("Refusing an untrusted attachment URL.")
        return self._open(url, accept)

    def _open(self, url, accept, method="GET", payload=None):
        headers = {
            "Accept": accept,
            "X-Redmine-API-Key": self.key,
            "User-Agent": "redmine-read-skill/1.0",
        }
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(url, method=method, headers=headers, data=body)
        try:
            response = self.opener.open(request, timeout=self.timeout)
        except HTTPError as exc:
            hints = {
                401: "API key rejected or REST API unavailable.",
                403: "Access forbidden for this user.",
                404: "Resource not found or hidden from this user.",
                429: "Rate limited; respect Retry-After before retrying.",
            }
            hint = "Redirect blocked; check the configured server." if 300 <= exc.code < 400 else hints.get(exc.code, "Server request failed.")
            if exc.code in (400, 422):
                try:
                    error_data = json.loads(exc.read(65536).decode("utf-8"))
                    errors = error_data.get("errors", [])
                    if isinstance(errors, list):
                        hint += " " + self.redact("; ".join(str(error) for error in errors))
                except (ValueError, UnicodeError, OSError, AttributeError):
                    pass
            raise RedmineError(f"HTTP {exc.code}: {hint}") from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            prefix = "Write outcome unknown; inspect the issue before retrying. " if method != "GET" else ""
            raise RedmineError(prefix + "Network/TLS request failed: " + self.redact(exc)) from exc
        self.last_url = url
        return response

    def get(self, path, params=None):
        path = path.lstrip("/")
        if (not re.fullmatch(r"[A-Za-z0-9_./~-]+\.json", path)
                or ".." in path or path.split("/", 1)[0].removesuffix(".json") not in RESOURCES):
            raise RedmineError("Use a relative JSON path of a standard Redmine resource.")
        params = params or {}
        if any(str(key).lower() in SECRET_PARAMS for key in params):
            raise RedmineError("Credentials must not be included in URL parameters.")
        url = self.base + "/" + path
        if params:
            url += "?" + urlencode(params, doseq=True)
        with self._open(url, "application/json") as response:
            if response.status != 200:
                raise RedmineError(f"Unexpected HTTP {response.status} for a GET request.")
            content_type = response.headers.get("Content-Type", "").lower()
            if "json" not in content_type:
                raise RedmineError("Expected JSON; server returned another format (possibly login HTML).")
            content = response.read(32 * 1024 * 1024 + 1)
            if len(content) > 32 * 1024 * 1024:
                raise RedmineError("JSON response exceeds 32 MiB; use narrower filters.")
        try:
            result = json.loads(content.decode("utf-8-sig"))
        except (UnicodeError, ValueError) as exc:
            raise RedmineError("Server response is not valid UTF-8 JSON.") from exc
        if not isinstance(result, dict):
            raise RedmineError("Expected a JSON object from Redmine.")
        return result

    def write(self, method, path, payload):
        if not ((method == "POST" and path == "issues.json") or
                (method == "PUT" and re.fullmatch(r"issues/[1-9][0-9]*\.json", path))):
            raise RedmineError("Unsupported mutation route; only create/update issue is allowed.")
        with self._open(self.base + "/" + path, "application/json", method, payload) as response:
            if response.status not in ((201,) if method == "POST" else (200, 204)):
                raise RedmineError(f"Unexpected HTTP {response.status}; inspect state before retrying.")
            content = response.read(32 * 1024 * 1024 + 1)
            if not content:
                return {}
            if len(content) > 32 * 1024 * 1024:
                raise RedmineError("Write accepted but response exceeded 32 MiB; inspect state before retrying.")
            try:
                result = json.loads(content.decode("utf-8-sig"))
                if not isinstance(result, dict):
                    raise ValueError("not an object")
                return result
            except (ValueError, UnicodeError) as exc:
                raise RedmineError("Write accepted but response was not JSON; inspect state before retrying.") from exc




def issue_id(value, base):
    if re.fullmatch(r"#?[0-9]+", value):
        result = int(value.lstrip("#"))
    else:
        parsed = urlparse(value)
        if origin(value) != origin(base) or parsed.username or parsed.password:
            raise RedmineError("Issue URL must use the configured HTTPS server.")
        expected = urlparse(base).path.rstrip("/") + r"/issues/([0-9]+)(?:\.json)?/?"
        match = re.fullmatch(expected, parsed.path)
        if not match:
            raise RedmineError("Expected issue number, #number or Redmine issue URL.")
        result = int(match.group(1))
    if result <= 0:
        raise RedmineError("Issue ID must be positive.")
    return result




def attachment(client, attachment_id, output, max_bytes):
    metadata = client.get(f"attachments/{attachment_id}.json").get("attachment")
    if not isinstance(metadata, dict) or not metadata.get("content_url"):
        raise RedmineError("Attachment metadata is missing content_url.")
    if int(metadata.get("filesize", 0)) > max_bytes:
        raise RedmineError("Attachment exceeds --max-bytes; choose an appropriate limit.")
    target = Path(output).expanduser().resolve()
    if target.exists():
        raise RedmineError("Output file already exists; choose a new filename.")
    if not target.parent.is_dir():
        raise RedmineError("Output directory does not exist.")
    temp_path = None
    try:
        with client.open_url(metadata["content_url"], "*/*") as response:
            if response.status != 200:
                raise RedmineError(f"Unexpected HTTP {response.status} for attachment.")
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
            if content_type == "text/html" and metadata.get("content_type") != "text/html":
                raise RedmineError("Attachment returned unexpected HTML (possibly login page).")
            length = int(response.headers.get("Content-Length", 0))
            if length > max_bytes:
                raise RedmineError("Attachment exceeds --max-bytes.")
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".redmine-", delete=False) as temp:
                temp_path = Path(temp.name)
                size = 0
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise RedmineError("Attachment exceeds --max-bytes.")
                    temp.write(chunk)
            if "filesize" in metadata and size != int(metadata["filesize"]):
                raise RedmineError("Downloaded size does not match attachment metadata.")
            # Atomic link refuses to replace an existing destination, even after a race.
            os.link(temp_path, target)
        return {"attachment_id": attachment_id, "filename": metadata.get("filename"),
                "saved_to": str(target), "bytes": size}
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
