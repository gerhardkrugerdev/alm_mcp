"""
Helix ALM MCP Server - Requirements and Document Management

An MCP server that connects to the Helix ALM REST API to create, read,
update, and manage requirements and requirement documents.
"""

import json
import re
import ssl
import base64
import os
import xml.etree.ElementTree as ET
import urllib.request
import urllib.parse
import urllib.error
from mcp.server.fastmcp import FastMCP

# --- Session configuration store ---
# Credentials are stored in memory only — never written to disk.
# Users call configure_helix_alm() at the start of each session.
# Environment variables serve as optional fallback defaults.

_session = {
    "helix_alm_url": os.environ.get("HELIX_ALM_URL", ""),
    "helix_alm_user": os.environ.get("HELIX_ALM_USER", ""),
    "helix_alm_password": os.environ.get("HELIX_ALM_PASSWORD", ""),
    "helix_alm_api_key": os.environ.get("HELIX_ALM_API_KEY", ""),
    "helix_alm_api_secret": os.environ.get("HELIX_ALM_API_SECRET", ""),
    "helix_alm_ssl_verify": os.environ.get("HELIX_ALM_SSL_VERIFY", "false").lower() == "true",
    "default_project": os.environ.get("HELIX_ALM_DEFAULT_PROJECT", ""),
    "azdo_org": os.environ.get("AZDO_ORG", ""),
    "azdo_project": os.environ.get("AZDO_PROJECT", ""),
    "azdo_pat": os.environ.get("AZDO_PAT", ""),
}


def _helix_configured() -> bool:
    """Check if Helix ALM connection is configured for this session."""
    if not _session["helix_alm_url"]:
        return False
    has_basic = bool(_session["helix_alm_user"] and _session["helix_alm_password"])
    has_apikey = bool(_session["helix_alm_api_key"] and _session["helix_alm_api_secret"])
    return has_basic or has_apikey


def _helix_not_configured_msg() -> str:
    return (
        "Helix ALM is not configured for this session. "
        "Please call the configure_helix_alm tool first with your server URL and credentials."
    )


def _get_helix_url() -> str:
    url = _session["helix_alm_url"]
    if url and not url.endswith("/"):
        url += "/"
    return url


def _resolve_project(project_name: str) -> str:
    """Return the given project name, or fall back to the session default."""
    return project_name or _session.get("default_project", "")


def _no_project_msg() -> str:
    return (
        "No project specified and no default project is set. "
        "Either pass a project_name or call set_default_project first."
    )


def _friendly_error(result: dict, action: str = "complete this action") -> str:
    """Translate an API error result into a human-readable message.

    Leads with plain English, then includes the server detail if available.
    """
    status = result.get("status", 0)
    data = result.get("data")

    # Extract the most useful detail from the response body
    detail = ""
    if isinstance(data, dict):
        detail = data.get("message", "") or data.get("error", "")
        if not detail and data.get("errors"):
            errors = data["errors"]
            if isinstance(errors, list):
                detail = "; ".join(
                    e.get("message", str(e)) if isinstance(e, dict) else str(e)
                    for e in errors[:3]
                )
        if not detail:
            detail = data.get("raw", "")
    if not detail:
        detail = result.get("message", "")

    # Map common HTTP status codes to friendly messages
    if status == 0:
        msg = f"Could not {action}: unable to reach the server. Check the URL and your network connection."
    elif status == 401:
        msg = f"Could not {action}: your credentials were rejected. Check your API key/token or username/password."
    elif status == 403:
        msg = f"Could not {action}: you don't have permission. Check your user role in the project."
    elif status == 404:
        msg = f"Could not {action}: the item was not found. Check that the name, tag, or ID is correct."
    elif status == 409:
        msg = f"Could not {action}: there was a conflict — the item may have been modified by someone else."
    elif status == 422:
        msg = f"Could not {action}: the server rejected the data. A required field may be missing or a value may be invalid."
    elif 400 <= status < 500:
        msg = f"Could not {action}: the request was invalid (status {status})."
    elif 500 <= status < 600:
        msg = f"Could not {action}: the server encountered an internal error (status {status}). Try again or contact your administrator."
    else:
        msg = f"Could not {action} (status {status})."

    if detail:
        # Truncate very long details but keep enough to be useful
        if len(detail) > 300:
            detail = detail[:300] + "..."
        msg += f" Detail: {detail}"

    return msg


mcp = FastMCP(
    "Helix ALM Requirements",
    instructions=(
        "MCP server for managing requirements, test cases, documents, and automation suites "
        "in Perforce ALM (formerly Helix ALM), with Azure DevOps integration.\n\n"
        "SETUP: Before using any tools, call configure_helix_alm with the server URL and "
        "API token. The user only needs the base URL (e.g. 'https://server:8443/') and their "
        "API token. For Azure DevOps, call configure_azure_devops with org, project, and PAT. "
        "Credentials are stored in memory only. If a tool returns 'not configured', prompt the "
        "user to call the appropriate configure tool.\n\n"
        "TIP: Use set_default_project so users don't have to specify a project on every call. "
        "If the server has only one project, it is auto-selected during configure_helix_alm.\n\n"
        "INTENT MAPPING — when the user says:\n"
        "- 'show/list my requirements' → list_requirements\n"
        "- 'find requirements about X' → search_requirements\n"
        "- 'show me requirement BR-123' → get_requirement\n"
        "- 'create a requirement' → create_requirement\n"
        "- 'update/change requirement BR-123' → update_requirement\n"
        "- 'approve/reject/comment on a requirement' → add_requirement_event\n"
        "- 'what can I do with this requirement' → get_requirement_workflow_events\n"
        "- 'what types of requirements are there' → get_requirement_types\n"
        "- 'what priority/status/category values can I use' → get_field_values\n"
        "- 'show/list my test cases' → list_test_cases\n"
        "- 'find test cases about X' → search_test_cases\n"
        "- 'show me test case TC-42' or 'show the steps' → get_test_case\n"
        "- 'create a test case with steps' → create_test_case\n"
        "- 'update test case / replace the steps' → update_test_case (note: replaces ALL steps)\n"
        "- 'add a step to test case' → add_test_case_steps (appends without removing existing steps)\n"
        "- 'what types of test cases are there' → get_test_case_types\n"
        "- 'link test case to requirement' → link_test_case_to_requirement\n"
        "- 'show my documents' → list_documents\n"
        "- 'show the document outline/tree' → get_document_tree\n"
        "- 'add requirements to a document' → add_to_document_tree or add_to_document_tree_top_level\n"
        "- 'show my automation suites' → list_automation_suites\n"
        "- 'submit test results' → submit_automation_results_simple (for quick pass/fail lists), "
        "submit_automation_build (full JSON), or submit_test_results_xml (from XML files)\n"
        "- 'import results from CI/CD' or 'from Azure DevOps' → azdo_submit_to_helix_alm or "
        "azdo_submit_latest_to_helix_alm\n"
        "- 'check connection' → get_connection_status\n\n"
        "Suites can be referenced by name (e.g. 'Regression Suite') or numeric ID. "
        "Requirements and test cases can be referenced by tag (e.g. 'BR-1960', 'TC-42') or numeric ID."
    ),
)

# --- HTTP helpers ---

def _get_auth_header(access_token: str | None = None) -> str:
    """Build the Authorization header value."""
    if access_token:
        return f"Bearer {access_token}"
    if _session["helix_alm_api_key"] and _session["helix_alm_api_secret"]:
        return f"ApiKey {_session['helix_alm_api_key']}:{_session['helix_alm_api_secret']}"
    creds = base64.b64encode(
        f"{_session['helix_alm_user']}:{_session['helix_alm_password']}".encode()
    ).decode()
    return f"Basic {creds}"


def _request(path: str, access_token: str | None = None,
             body: dict | None = None, method: str | None = None) -> dict:
    """Send a request to the Helix ALM REST API and return parsed JSON."""
    url = _get_helix_url() + path
    req = urllib.request.Request(url)
    req.add_header("Authorization", _get_auth_header(access_token))

    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")

    if method is not None:
        req.method = method

    ctx = ssl.create_default_context()
    if not _session["helix_alm_ssl_verify"]:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    try:
        resp = urllib.request.urlopen(req, context=ctx)
        data = resp.read()
        resp.close()
        if data:
            return {"status": resp.status, "data": json.loads(data.decode())}
        return {"status": resp.status, "data": None}
    except urllib.error.HTTPError as e:
        err_body = e.fp.read()
        try:
            err_data = json.loads(err_body.decode())
        except Exception:
            err_data = {"raw": err_body.decode()}
        return {"status": e.code, "data": err_data, "error": True}
    except urllib.error.URLError as e:
        return {"status": 0, "data": None,
                "error": True, "message": str(e.reason)}
    except Exception as e:
        return {"status": 0, "data": None,
                "error": True, "message": str(e)}


def _encode_project(project_name: str) -> str:
    return urllib.parse.quote(project_name, safe="")


def _get_token(project_name: str) -> str | None:
    """Obtain a bearer access token for a project.
    Returns None if Helix ALM is not configured or token request fails."""
    if not _helix_configured():
        return None
    result = _request(f"{_encode_project(project_name)}/token")
    if result.get("error"):
        return None
    return result["data"].get("accessToken") if result["data"] else None


# --- Steps helpers ---

def _build_steps_payload(steps_json: str) -> dict | str:
    """Parse steps_json and build the stepsData payload for the Helix ALM API.
    Returns the stepsData dict on success, or an error string on failure."""
    try:
        steps_input = json.loads(steps_json)
        if not isinstance(steps_input, list):
            return "Error: steps_json must be a JSON array."
        detailed_steps = []
        for i, s in enumerate(steps_input, start=1):
            step_rows = []
            if s.get("expectedResult"):
                step_rows.append({
                    "type": "expectedResult",
                    "expectedResult": {
                        "text": s["expectedResult"],
                        "fileReferences": [],
                    },
                })
            detailed_steps.append({
                "type": "step",
                "step": {
                    "number": i,
                    "text": s.get("text", ""),
                    "stepRows": step_rows,
                },
            })
        return {
            "stepsData": {
                "type": "detailed",
                "modifiedToCorrectSyntax": False,
                "basic": [],
                "detailed": detailed_steps,
            }
        }
    except json.JSONDecodeError:
        return "Error: steps_json must be a valid JSON string."


def _put_steps(project_name: str, token: str, tc_id: int, steps_payload: dict) -> str | None:
    """PUT steps to a test case via the /steps sub-resource. Returns error string or None on success."""
    proj = _encode_project(project_name)
    result = _request(f"{proj}/testCases/{tc_id}/steps", token, steps_payload, "PUT")
    if result.get("error"):
        return _friendly_error(result, "add steps to the test case")
    return None


# --- Field helpers ---

def _get_field(fields: list, label: str):
    """Get the value of a field by label from a Helix ALM fields list."""
    for f in fields:
        if f.get("label") == label:
            ftype = f.get("type")
            if ftype == "formattedString":
                return f.get("formattedString", {}).get("text", "")
            if ftype == "menuItem":
                return f.get("menuItem", {}).get("label", "")
            if ftype == "user":
                return f.get("user", {}).get("username", "")
            return f.get(ftype)
    return None


def _set_field(fields: list, label: str, value: str) -> bool:
    """Set a field value by label. Returns True if field was found."""
    for f in fields:
        if f.get("label") == label:
            ftype = f.get("type")
            if ftype == "formattedString":
                f["formattedString"] = {"isFormatted": False, "text": value}
            elif ftype == "menuItem":
                f["menuItem"] = {"label": value}
            elif ftype == "user":
                f["user"] = {"username": value}
            else:
                f[ftype] = value
            return True
    return False


def _format_requirement(req: dict) -> dict:
    """Format a requirement into a readable summary."""
    fields = req.get("fields", [])
    summary = {
        "id": req.get("id"),
        "tag": req.get("tag", ""),
    }
    # Extract common fields
    for label in ["Summary", "Description", "Priority", "Status",
                  "Currently Assigned To", "Type", "Category"]:
        val = _get_field(fields, label)
        if val is not None:
            summary[label.lower().replace(" ", "_")] = val
    return summary


def _format_test_case(tc: dict) -> dict:
    """Format a test case into a readable summary."""
    fields = tc.get("fields", [])
    summary = {
        "id": tc.get("id"),
        "tag": tc.get("tag", ""),
    }
    for label in ["Summary", "Description", "Priority", "Status",
                  "Currently Assigned To", "Type"]:
        val = _get_field(fields, label)
        if val is not None:
            summary[label.lower().replace(" ", "_")] = val
    return summary


def _resolve_requirement_id(project_name: str, token: str, identifier: str) -> int | None:
    """Resolve a requirement tag (e.g. 'BR-1960') or numeric ID to the internal API id.

    Users typically reference requirements by tag, but the API uses internal IDs.
    Optimized to minimize payload by requesting only the Tag column.
    """
    # If it's purely numeric, try it as a direct ID first
    if identifier.isdigit():
        proj = _encode_project(project_name)
        result = _request(f"{proj}/requirements/{identifier}", token)
        if not result.get("error"):
            return int(identifier)

    # Fetch the list with minimal columns — tag and id are always top-level fields
    proj = _encode_project(project_name)
    result = _request(f"{proj}/requirements?columns={urllib.parse.quote('Tag')}", token)
    if result.get("error"):
        return None

    for req in result["data"].get("requirements", []):
        if req.get("tag", "").upper() == identifier.upper():
            return req["id"]
        # Also match just the number part (e.g. "1960" matches "BR-1960")
        tag = req.get("tag", "")
        if "-" in tag and tag.split("-", 1)[1] == identifier:
            return req["id"]

    return None


def _resolve_test_case_id(project_name: str, token: str, identifier: str) -> int | None:
    """Resolve a test case tag (e.g. 'TC-42') or numeric ID to the internal API id.

    Optimized to minimize payload by requesting only the Tag column.
    """
    if identifier.isdigit():
        proj = _encode_project(project_name)
        result = _request(f"{proj}/testCases/{identifier}", token)
        if not result.get("error"):
            return int(identifier)

    # Fetch the list with minimal columns — tag and id are always top-level fields
    proj = _encode_project(project_name)
    result = _request(f"{proj}/testCases?columns={urllib.parse.quote('Tag')}", token)
    if result.get("error"):
        return None

    for tc in result["data"].get("testCases", []):
        if tc.get("tag", "").upper() == identifier.upper():
            return tc["id"]
        tag = tc.get("tag", "")
        if "-" in tag and tag.split("-", 1)[1] == identifier:
            return tc["id"]

    return None


# --- Configuration MCP Tools ---

@mcp.tool()
def configure_helix_alm(
    url: str,
    api_token: str = "",
    username: str = "",
    password: str = "",
    ssl_verify: bool = False,
    default_project: str = "",
) -> str:
    """Configure the Helix ALM connection for this session. Credentials are stored
    in memory only and never written to disk.

    Provide EITHER an api_token (recommended) OR username+password for authentication.
    The URL only needs the server base (e.g. 'https://your-server:8443/') — the REST API
    path is appended automatically.

    Args:
        url: Helix ALM server URL (e.g. 'https://your-server:8443/'). The '/helix-alm/api/v0/' path is added automatically if not present.
        api_token: API token in 'key:secret' format (recommended). Generate this from Helix ALM Admin under API Keys.
        username: Username for basic authentication (alternative to api_token).
        password: Password for basic authentication (alternative to api_token).
        ssl_verify: Whether to verify SSL certificates (default False for self-signed certs).
        default_project: Optional default project name. When set, you can omit project_name from other tools.
    """
    if not url:
        return "Error: url is required."

    # Parse api_token into key and secret
    api_key = ""
    api_secret = ""
    if api_token:
        parts = api_token.split(":", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return "Error: api_token must be in 'key:secret' format (two values separated by a colon)."
        api_key, api_secret = parts[0], parts[1]

    has_basic = bool(username and password)
    has_apikey = bool(api_key and api_secret)
    if not has_basic and not has_apikey:
        return "Error: Provide either an api_token or username+password."

    # Normalize the URL — strip any partial REST API path, then append the canonical one.
    # This handles users pasting URLs like:
    #   https://server:8443/
    #   https://server:8443/helix-alm/
    #   https://server:8443/helix-alm/api/
    #   https://server:8443/helix-alm/api/v0
    #   https://server:8443/helix-alm/api/v0/
    normalized_url = url.rstrip("/")
    for suffix in ["/helix-alm/api/v0", "/helix-alm/api", "/helix-alm"]:
        if normalized_url.lower().endswith(suffix):
            normalized_url = normalized_url[:-len(suffix)]
            break
    normalized_url += "/helix-alm/api/v0/"

    _session["helix_alm_url"] = normalized_url
    _session["helix_alm_user"] = username
    _session["helix_alm_password"] = password
    _session["helix_alm_api_key"] = api_key
    _session["helix_alm_api_secret"] = api_secret
    _session["helix_alm_ssl_verify"] = ssl_verify
    if default_project:
        _session["default_project"] = default_project

    # Verify connectivity
    result = _request("projects")
    if result.get("error"):
        return json.dumps({
            "status": "configured_but_connection_failed",
            "url": normalized_url,
            "auth_method": "api_token" if has_apikey else "basic",
            "error": result,
        }, indent=2)

    projects = result["data"].get("projects", [])
    project_names = [p["name"] for p in projects]

    # Auto-set default project if there is exactly one project
    if not _session["default_project"] and len(project_names) == 1:
        _session["default_project"] = project_names[0]

    response = {
        "status": "connected",
        "url": normalized_url,
        "auth_method": "api_token" if has_apikey else "basic",
        "available_projects": project_names,
    }
    if _session["default_project"]:
        response["default_project"] = _session["default_project"]
    return json.dumps(response, indent=2)


@mcp.tool()
def configure_azure_devops(
    organization: str,
    project: str,
    pat: str,
) -> str:
    """Configure the Azure DevOps connection for this session. Credentials are stored
    in memory only and never written to disk.

    Args:
        organization: Azure DevOps organization name.
        project: Azure DevOps project name.
        pat: Personal access token with read access to builds and test results.
    """
    if not organization or not project or not pat:
        return "Error: organization, project, and pat are all required."

    _session["azdo_org"] = organization
    _session["azdo_project"] = project
    _session["azdo_pat"] = pat

    # Verify connectivity
    base = _azdo_base_url()
    result = _azdo_request(f"{base}/_apis/projects?api-version=7.1")
    if result.get("error"):
        return json.dumps({
            "status": "configured_but_connection_failed",
            "organization": organization,
            "project": project,
            "error": result,
        }, indent=2)

    return json.dumps({
        "status": "connected",
        "organization": organization,
        "project": project,
    }, indent=2)


@mcp.tool()
def get_connection_status() -> str:
    """Check the current connection status for Helix ALM and Azure DevOps.
    Shows whether each service is configured for this session (without revealing credentials)."""
    status = {
        "helix_alm": {
            "configured": _helix_configured(),
            "url": _session["helix_alm_url"] or "(not set)",
            "auth_method": (
                "api_key" if _session["helix_alm_api_key"] and _session["helix_alm_api_secret"]
                else "basic" if _session["helix_alm_user"] and _session["helix_alm_password"]
                else "none"
            ),
            "ssl_verify": _session["helix_alm_ssl_verify"],
            "default_project": _session["default_project"] or "(not set)",
        },
        "azure_devops": {
            "configured": _azdo_configured(),
            "organization": _session["azdo_org"] or "(not set)",
            "project": _session["azdo_project"] or "(not set)",
        },
    }
    return json.dumps(status, indent=2)


@mcp.tool()
def set_default_project(project_name: str) -> str:
    """Set the default Helix ALM project for this session. Once set, you can omit
    project_name from all other tools and they will use this project automatically.

    Args:
        project_name: Name of the Helix ALM project to use as the default.
    """
    if not _helix_configured():
        return _helix_not_configured_msg()

    # Verify the project exists
    result = _request("projects")
    if result.get("error"):
        return _friendly_error(result, "check available projects")

    project_names = [p["name"] for p in result["data"].get("projects", [])]
    if project_name not in project_names:
        return json.dumps({
            "error": f"Project '{project_name}' not found.",
            "available_projects": project_names,
        }, indent=2)

    _session["default_project"] = project_name
    return json.dumps({
        "message": f"Default project set to '{project_name}'",
        "default_project": project_name,
    }, indent=2)


# --- MCP Tools ---

@mcp.tool()
def list_projects() -> str:
    """List all available projects in Helix ALM."""
    if not _helix_configured():
        return _helix_not_configured_msg()
    result = _request("projects")
    if result.get("error"):
        return _friendly_error(result, "list projects")
    projects = result["data"].get("projects", [])
    names = [p["name"] for p in projects]
    return json.dumps({"projects": names}, indent=2)


@mcp.tool()
def list_requirements(project_name: str = "", columns: str = "", filter_name: str = "") -> str:
    """List requirements in a Helix ALM project.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        columns: Comma-separated field labels to include (e.g. "Summary,Priority"). Leave empty for defaults.
        filter_name: Optional saved filter name to apply.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    proj = _encode_project(project_name)
    params = []
    if columns:
        params.append(f"columns={urllib.parse.quote(columns)}")
    if filter_name:
        params.append(f"filter={urllib.parse.quote(filter_name)}")
    qs = ("?" + "&".join(params)) if params else ""

    result = _request(f"{proj}/requirements{qs}", token)
    if result.get("error"):
        return _friendly_error(result, "list requirements")

    reqs = result["data"].get("requirements", [])
    formatted = [_format_requirement(r) for r in reqs]
    return json.dumps({"count": len(formatted), "requirements": formatted}, indent=2)


@mcp.tool()
def get_requirement(project_name: str = "", requirement_identifier: str = "") -> str:
    """Get a single requirement by tag (e.g. 'BR-1960') or internal ID.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        requirement_identifier: The requirement tag (e.g. 'BR-1960', 'FR-1961') or numeric ID.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    req_id = _resolve_requirement_id(project_name, token, requirement_identifier)
    if req_id is None:
        return f"Error: Could not find requirement '{requirement_identifier}'."

    proj = _encode_project(project_name)
    result = _request(f"{proj}/requirements/{req_id}", token)
    if result.get("error"):
        return _friendly_error(result, f"get requirement '{requirement_identifier}'")

    req = result["data"]
    summary = _format_requirement(req)
    all_fields = {}
    for f in req.get("fields", []):
        all_fields[f["label"]] = _get_field(req["fields"], f["label"])
    summary["all_fields"] = all_fields
    return json.dumps(summary, indent=2)


@mcp.tool()
def get_requirement_types(project_name: str = "") -> str:
    """Get the requirement types currently in use in a project (e.g. Business Requirement,
    Functional Requirement). Note: this discovers types from existing requirements, so types
    that haven't been used yet may not appear.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    proj = _encode_project(project_name)
    # Fetch with minimal columns — requirementType is a top-level field, always included
    result = _request(f"{proj}/requirements?columns={urllib.parse.quote('Tag')}", token)
    if result.get("error"):
        return _friendly_error(result, "get requirement types")

    types_seen = {}
    for req in result["data"].get("requirements", []):
        rt = req.get("requirementType", {})
        if rt and rt.get("label"):
            types_seen[rt["label"]] = rt.get("id")

    return json.dumps({"requirement_types": [
        {"label": label, "id": rid} for label, rid in types_seen.items()
    ]}, indent=2)


@mcp.tool()
def get_field_values(
    project_name: str = "",
    item_type: str = "requirements",
    field_label: str = "",
) -> str:
    """Discover the values currently in use for a specific field on requirements or test cases.
    Useful for finding valid Priority, Status, Category, or other menu field values before
    creating or updating items.

    Note: this discovers values from existing items, so values that haven't been used yet
    may not appear.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        item_type: The type of item to scan — 'requirements' or 'testCases'.
        field_label: The field name to discover values for (e.g. 'Priority', 'Status', 'Category', 'Type').
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    if not field_label:
        return "Error: field_label is required. Examples: 'Priority', 'Status', 'Category', 'Type'."
    if item_type not in ("requirements", "testCases"):
        return "Error: item_type must be 'requirements' or 'testCases'."

    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    proj = _encode_project(project_name)
    # Request only the specific field column to minimize payload
    qs = f"?columns={urllib.parse.quote(field_label)}"
    result = _request(f"{proj}/{item_type}{qs}", token)
    if result.get("error"):
        # If the column request fails, retry without column filter
        result = _request(f"{proj}/{item_type}", token)
        if result.get("error"):
            return _friendly_error(result, f"get field values for '{field_label}'")

    items_key = item_type  # API returns {"requirements": [...]} or {"testCases": [...]}
    items = result["data"].get(items_key, [])

    values_seen = set()
    field_found = False
    for item in items:
        for f in item.get("fields", []):
            if f.get("label") == field_label:
                field_found = True
                val = _get_field(item["fields"], field_label)
                if val is not None and val != "":
                    values_seen.add(val)

    if not field_found:
        # List available field labels to help the user
        all_labels = set()
        for item in items[:5]:  # Sample from first few items
            for f in item.get("fields", []):
                if f.get("label"):
                    all_labels.add(f["label"])
        return json.dumps({
            "error": f"Field '{field_label}' not found on any {item_type}.",
            "available_fields": sorted(all_labels),
        }, indent=2)

    return json.dumps({
        "field": field_label,
        "item_type": item_type,
        "values": sorted(values_seen),
        "count": len(values_seen),
    }, indent=2)


@mcp.tool()
def create_requirement(project_name: str = "", summary: str = "", description: str = "",
                       requirement_type: str = "Functional Requirement",
                       priority: str = "",
                       additional_fields: str = "") -> str:
    """Create a new requirement in a Helix ALM project.

    IMPORTANT: If this requirement will be added to a requirement document,
    you MUST call create_document_snapshot for that document BEFORE making
    this change so a point-in-time baseline exists.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        summary: The requirement summary/title.
        description: Detailed description of the requirement.
        requirement_type: Requirement type label (e.g. "Business Requirement", "Functional Requirement", "Non-Functional Requirement"). Use get_requirement_types to see available types.
        priority: Priority level (e.g. "High", "Medium", "Low"). Must match project config.
        additional_fields: JSON string of extra fields to set, e.g. '{"Category": "Security"}'.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    proj = _encode_project(project_name)

    # Build the requirement body
    body = {
        "requirementType": {"label": requirement_type},
        "fields": [
            {
                "label": "Summary",
                "type": "string",
                "string": summary,
            },
        ]
    }
    if description:
        body["fields"].append({
            "label": "Description",
            "type": "formattedString",
            "formattedString": {
                "isFormatted": False,
                "text": description,
            },
        })
    if priority:
        body["fields"].append({
            "label": "Priority",
            "type": "menuItem",
            "menuItem": {"label": priority},
        })

    # Apply any additional fields
    if additional_fields:
        try:
            extras = json.loads(additional_fields)
            for label, value in extras.items():
                if not any(f["label"] == label for f in body["fields"]):
                    body["fields"].append({
                        "label": label,
                        "type": "string",
                        "string": value,
                    })
                else:
                    _set_field(body["fields"], label, value)
        except json.JSONDecodeError:
            return "Error: additional_fields must be a valid JSON string."

    # API expects body wrapped in a "requirements" array
    wrapped_body = {"requirements": [body]}
    result = _request(f"{proj}/requirements", token, wrapped_body, "POST")

    data = result.get("data", {})
    # Check for partial success (206) with errors
    if result.get("error") or (isinstance(data, dict) and data.get("errors")):
        errors = data.get("errors", []) if isinstance(data, dict) else []
        created = data.get("requirements", []) if isinstance(data, dict) else []
        if created:
            return json.dumps({
                "message": "Requirement created with warnings",
                "requirement": _format_requirement(created[0]),
                "warnings": errors,
            }, indent=2)
        return _friendly_error(result, "create the requirement")

    created_list = data.get("requirements", []) if isinstance(data, dict) else []
    created = created_list[0] if created_list else data
    return json.dumps({
        "message": "Requirement created successfully",
        "requirement": _format_requirement(created) if created else {},
    }, indent=2)


@mcp.tool()
def update_requirement(project_name: str = "", requirement_identifier: str = "",
                       summary: str = "", description: str = "",
                       priority: str = "", additional_fields: str = "") -> str:
    """Update an existing requirement in Helix ALM.

    IMPORTANT: If this requirement belongs to a requirement document, you MUST
    call create_document_snapshot for that document BEFORE making this change
    so a point-in-time baseline exists.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        requirement_identifier: The requirement tag (e.g. 'BR-1960') or numeric ID.
        summary: New summary (leave empty to keep current).
        description: New description (leave empty to keep current).
        priority: New priority (leave empty to keep current).
        additional_fields: JSON string of extra fields to update, e.g. '{"Category": "Security"}'.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    req_id = _resolve_requirement_id(project_name, token, requirement_identifier)
    if req_id is None:
        return f"Error: Could not find requirement '{requirement_identifier}'."

    proj = _encode_project(project_name)

    get_result = _request(f"{proj}/requirements/{req_id}", token)
    if get_result.get("error"):
        return _friendly_error(get_result, f"get requirement '{requirement_identifier}' for updating")

    req = get_result["data"]
    fields = req.get("fields", [])

    if summary:
        _set_field(fields, "Summary", summary)
    if description:
        _set_field(fields, "Description", description)
    if priority:
        _set_field(fields, "Priority", priority)

    if additional_fields:
        try:
            extras = json.loads(additional_fields)
            for label, value in extras.items():
                _set_field(fields, label, value)
        except json.JSONDecodeError:
            return "Error: additional_fields must be a valid JSON string."

    result = _request(f"{proj}/requirements/{req_id}", token, req, "PUT")
    if result.get("error"):
        return _friendly_error(result, "update the requirement")

    updated = result["data"] if result["data"] else req
    return json.dumps({
        "message": f"Requirement {requirement_identifier} updated successfully",
        "requirement": _format_requirement(updated),
    }, indent=2)


@mcp.tool()
def delete_requirement(project_name: str = "", requirement_identifier: str = "") -> str:
    """Delete a requirement from a Helix ALM project.

    IMPORTANT: If this requirement belongs to a requirement document, you MUST
    call create_document_snapshot for that document BEFORE making this change
    so a point-in-time baseline exists.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        requirement_identifier: The requirement tag (e.g. 'BR-1960') or numeric ID.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    req_id = _resolve_requirement_id(project_name, token, requirement_identifier)
    if req_id is None:
        return f"Error: Could not find requirement '{requirement_identifier}'."

    proj = _encode_project(project_name)
    result = _request(f"{proj}/requirements/{req_id}", token, method="DELETE")
    if result.get("error"):
        return _friendly_error(result, f"delete requirement '{requirement_identifier}'")

    return json.dumps({"message": f"Requirement {requirement_identifier} deleted successfully"})


@mcp.tool()
def add_requirement_event(project_name: str = "", requirement_identifier: str = "",
                          event_name: str = "", notes: str = "") -> str:
    """Add a workflow event to a requirement (e.g. Comment, Approve, Reject).

    IMPORTANT: If this requirement belongs to a requirement document, you MUST
    call create_document_snapshot for that document BEFORE making this change
    so a point-in-time baseline exists.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        requirement_identifier: The requirement tag (e.g. 'BR-1960') or numeric ID.
        event_name: The workflow event name (e.g. "Comment", "Approve", "Reject").
        notes: Optional notes/comments for the event.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    req_id = _resolve_requirement_id(project_name, token, requirement_identifier)
    if req_id is None:
        return f"Error: Could not find requirement '{requirement_identifier}'."

    proj = _encode_project(project_name)
    event_data = {
        "eventsData": [{
            "name": event_name,
            "fields": [],
        }]
    }
    if notes:
        event_data["eventsData"][0]["fields"].append({
            "label": "Notes",
            "type": "string",
            "string": notes,
        })

    result = _request(f"{proj}/requirements/{req_id}/events", token, event_data, "POST")
    if result.get("error"):
        return _friendly_error(result, f"add event '{event_name}' to the requirement")

    return json.dumps({
        "message": f"Event '{event_name}' added to requirement {requirement_identifier}",
        "data": result.get("data"),
    }, indent=2)


@mcp.tool()
def search_requirements(project_name: str = "", search_text: str = "") -> str:
    """Search requirements by text across Summary and Description fields.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        search_text: Text to search for in requirements.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    proj = _encode_project(project_name)
    # Use the filter/search query parameter
    qs = f"?searchText={urllib.parse.quote(search_text)}"
    result = _request(f"{proj}/requirements{qs}", token)
    if result.get("error"):
        return _friendly_error(result, "search requirements")

    reqs = result["data"].get("requirements", [])
    formatted = [_format_requirement(r) for r in reqs]
    return json.dumps({"count": len(formatted), "requirements": formatted}, indent=2)


@mcp.tool()
def get_requirement_workflow_events(project_name: str = "", requirement_identifier: str = "") -> str:
    """Get the available workflow events for a requirement.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        requirement_identifier: The requirement tag (e.g. 'BR-1960') or numeric ID.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    req_id = _resolve_requirement_id(project_name, token, requirement_identifier)
    if req_id is None:
        return f"Error: Could not find requirement '{requirement_identifier}'."

    proj = _encode_project(project_name)
    result = _request(f"{proj}/requirements/{req_id}/availableEvents", token)
    if result.get("error"):
        return _friendly_error(result, "get workflow events for the requirement")

    data = result["data"]
    events_list = data.get("eventsData", data.get("events", []))
    if isinstance(events_list, list):
        formatted_events = []
        for event in events_list:
            entry = {"name": event.get("name", "")}
            fields = event.get("fields", [])
            if fields:
                entry["fields"] = [f.get("label", "") for f in fields if f.get("label")]
            formatted_events.append(entry)
        return json.dumps({
            "requirement": requirement_identifier,
            "available_events": formatted_events,
            "count": len(formatted_events),
        }, indent=2)

    return json.dumps(data, indent=2)


# --- Test Case MCP Tools ---

@mcp.tool()
def create_test_case(
    project_name: str = "",
    summary: str = "",
    test_case_type: str = "Validation",
    description: str = "",
    priority: str = "",
    steps_json: str = "",
    additional_fields: str = "",
) -> str:
    """Create a new test case in a Helix ALM project.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        summary: The test case summary/title.
        test_case_type: The Type field value (e.g. "Validation", "Functional", "Regression").
                        Must match a menu item configured in the project. Defaults to "Validation".
        description: Detailed description of the test case.
        priority: Priority level (e.g. "High", "Medium", "Low"). Must match project config.
        steps_json: Optional JSON array of test steps. Each step needs 'text' and optionally
                    'expectedResult'. Example: '[{"text": "Open login page", "expectedResult": "Login page displayed"}]'
        additional_fields: JSON string of extra fields to set, e.g. '{"Automated Test": true}'.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    proj = _encode_project(project_name)

    body: dict = {
        "fields": [
            {
                "label": "Summary",
                "type": "string",
                "string": summary,
            },
            {
                "label": "Type",
                "type": "menuItem",
                "menuItem": {"label": test_case_type},
            },
        ]
    }

    if description:
        body["fields"].append({
            "label": "Description",
            "type": "formattedString",
            "formattedString": {
                "isFormatted": False,
                "text": description,
            },
        })

    if priority:
        body["fields"].append({
            "label": "Priority",
            "type": "menuItem",
            "menuItem": {"label": priority},
        })

    if additional_fields:
        try:
            extras = json.loads(additional_fields)
            for label, value in extras.items():
                if not any(f["label"] == label for f in body["fields"]):
                    body["fields"].append({
                        "label": label,
                        "type": "string",
                        "string": value,
                    })
                else:
                    _set_field(body["fields"], label, value)
        except json.JSONDecodeError:
            return "Error: additional_fields must be a valid JSON string."

    # Parse steps ahead of time (but don't add inline — API ignores inline steps)
    steps_payload = None
    if steps_json:
        steps_payload = _build_steps_payload(steps_json)
        if isinstance(steps_payload, str):
            return steps_payload  # Error message

    wrapped_body = {"testCases": [body]}
    result = _request(f"{proj}/testCases", token, wrapped_body, "POST")

    data = result.get("data", {})
    if result.get("error") or (isinstance(data, dict) and data.get("errors")):
        errors = data.get("errors", []) if isinstance(data, dict) else []
        created = data.get("testCases", []) if isinstance(data, dict) else []
        if created:
            tc = created[0]
            # Even with warnings, try to add steps if test case was created
            if steps_payload and tc.get("id"):
                _put_steps(project_name, token, tc["id"], steps_payload)
            return json.dumps({
                "message": "Test case created with warnings",
                "test_case": {"id": tc.get("id"), "tag": tc.get("tag", ""), "summary": summary},
                "warnings": errors,
            }, indent=2)
        return _friendly_error(result, "create the test case")

    created_list = data.get("testCases", []) if isinstance(data, dict) else []
    created = created_list[0] if created_list else data
    tc_id = created.get("id") if created else None
    tc_tag = created.get("tag", "") if created else ""

    # Add steps via PUT to the /steps sub-resource (API ignores steps in POST body)
    steps_msg = ""
    if steps_payload and tc_id:
        err = _put_steps(project_name, token, tc_id, steps_payload)
        if err:
            steps_msg = f" (Warning: test case created but steps failed: {err})"
        else:
            steps_msg = f" with {len(steps_payload['stepsData']['detailed'])} steps"

    return json.dumps({
        "message": f"Test case created successfully{steps_msg}",
        "test_case": {
            "id": tc_id,
            "tag": tc_tag,
            "summary": summary,
        },
    }, indent=2)


@mcp.tool()
def get_test_case(
    project_name: str = "",
    test_case_identifier: str = "",
) -> str:
    """Get a single test case by tag (e.g. 'TC-382') or internal ID, including steps.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        test_case_identifier: The test case tag (e.g. 'TC-382') or numeric ID.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    tc_id = _resolve_test_case_id(project_name, token, test_case_identifier)
    if tc_id is None:
        return f"Error: Could not find test case '{test_case_identifier}'."

    proj = _encode_project(project_name)
    result = _request(f"{proj}/testCases/{tc_id}", token)
    if result.get("error"):
        return _friendly_error(result, "retrieve the test case")

    tc = result["data"]
    fields = tc.get("fields", [])

    summary = {
        "id": tc.get("id"),
        "tag": tc.get("tag", ""),
    }
    for label in ["Summary", "Description", "Type", "Status",
                  "Currently Assigned To", "Priority"]:
        val = _get_field(fields, label)
        if val is not None:
            summary[label.lower().replace(" ", "_")] = val

    # Include all fields for completeness
    all_fields = {}
    for f in fields:
        label = f.get("label", "")
        val = _get_field(fields, label)
        if val is not None:
            all_fields[label] = val
    summary["all_fields"] = all_fields

    # Fetch steps from the sub-resource
    steps_result = _request(f"{proj}/testCases/{tc_id}/steps", token)
    if not steps_result.get("error") and steps_result.get("data"):
        steps_data = steps_result["data"].get("stepsData", {})
        detailed = steps_data.get("detailed", [])
        basic = steps_data.get("basic", [])
        step_list = detailed if detailed else basic
        formatted_steps = []
        for s in step_list:
            step = s.get("step", {})
            step_info = {
                "number": step.get("number"),
                "text": step.get("text", ""),
            }
            for row in step.get("stepRows", []):
                if row.get("type") == "expectedResult":
                    step_info["expectedResult"] = row["expectedResult"].get("text", "")
            formatted_steps.append(step_info)
        summary["steps"] = formatted_steps
        summary["step_count"] = len(formatted_steps)
    else:
        summary["steps"] = []
        summary["step_count"] = 0

    # Include links info
    links_data = tc.get("links", {}).get("linksData", [])
    if links_data:
        summary["linked_items"] = []
        for link in links_data:
            link_info = {"link_type": link.get("linkDefinition", {}).get("name", "")}
            if link.get("type") == "parentChildren":
                pc = link.get("parentChildren", {})
                parent = pc.get("parent", {})
                children = pc.get("children", [])
                if parent:
                    link_info["parent"] = {"itemType": parent.get("itemType"), "itemID": parent.get("itemID")}
                if children:
                    link_info["children"] = [{"itemType": c.get("itemType"), "itemID": c.get("itemID")} for c in children]
            summary["linked_items"].append(link_info)

    return json.dumps(summary, indent=2)


@mcp.tool()
def update_test_case(
    project_name: str = "",
    test_case_identifier: str = "",
    summary: str = "",
    description: str = "",
    test_case_type: str = "",
    steps_json: str = "",
    additional_fields: str = "",
) -> str:
    """Update an existing test case in Helix ALM, including its steps.

    WARNING: If steps_json is provided, it REPLACES ALL existing steps — it does not
    append. To add steps without losing existing ones, use add_test_case_steps instead.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        test_case_identifier: The test case tag (e.g. 'TC-382') or numeric ID.
        summary: New summary (leave empty to keep current).
        description: New description (leave empty to keep current).
        test_case_type: New Type field value (leave empty to keep current).
        steps_json: JSON array of steps that will REPLACE all existing steps.
                    To add steps without losing existing ones, use add_test_case_steps instead.
                    Each step needs 'text' and optionally 'expectedResult'.
                    Example: '[{"text": "Open login page", "expectedResult": "Login page displayed"}]'
        additional_fields: JSON string of extra fields to update, e.g. '{"Category": "Security"}'.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    tc_id = _resolve_test_case_id(project_name, token, test_case_identifier)
    if tc_id is None:
        return f"Error: Could not find test case '{test_case_identifier}'."

    proj = _encode_project(project_name)

    # Build fields update body
    fields_to_update = []
    if summary:
        fields_to_update.append({"label": "Summary", "type": "string", "string": summary})
    if description:
        fields_to_update.append({
            "label": "Description",
            "type": "formattedString",
            "formattedString": {"isFormatted": False, "text": description},
        })
    if test_case_type:
        fields_to_update.append({"label": "Type", "type": "menuItem", "menuItem": {"label": test_case_type}})

    if additional_fields:
        try:
            extras = json.loads(additional_fields)
            for label, value in extras.items():
                fields_to_update.append({"label": label, "type": "string", "string": value})
        except json.JSONDecodeError:
            return "Error: additional_fields must be a valid JSON string."

    messages = []

    # Update fields via PUT if any fields changed
    if fields_to_update:
        body = {"id": tc_id, "fields": fields_to_update}
        wrapped = {"testCases": [body]}
        result = _request(f"{proj}/testCases", token, wrapped, "PUT")
        if result.get("error"):
            return _friendly_error(result, "update test case fields")
        messages.append("fields updated")

    # Update steps via PUT to /steps sub-resource
    if steps_json:
        steps_payload = _build_steps_payload(steps_json)
        if isinstance(steps_payload, str):
            return steps_payload  # Error message
        err = _put_steps(project_name, token, tc_id, steps_payload)
        if err:
            return err
        step_count = len(steps_payload["stepsData"]["detailed"])
        messages.append(f"{step_count} steps updated")

    if not messages:
        return "No changes specified. Provide at least one field or steps_json to update."

    return json.dumps({
        "message": f"Test case {test_case_identifier} updated successfully ({', '.join(messages)})",
        "test_case_id": tc_id,
    }, indent=2)


@mcp.tool()
def add_test_case_steps(
    project_name: str = "",
    test_case_identifier: str = "",
    steps_json: str = "",
) -> str:
    """Add steps to an existing test case WITHOUT removing its current steps.
    The new steps are appended after the existing ones.

    Use this instead of update_test_case when you want to add steps to a test case
    that already has steps, without losing the existing ones.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        test_case_identifier: The test case tag (e.g. 'TC-382') or numeric ID.
        steps_json: JSON array of new steps to append. Each step needs 'text' and optionally
                    'expectedResult'. Example: '[{"text": "Click Save", "expectedResult": "Record is saved"}]'
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    tc_id = _resolve_test_case_id(project_name, token, test_case_identifier)
    if tc_id is None:
        return f"Error: Could not find test case '{test_case_identifier}'."

    if not steps_json:
        return "Error: steps_json is required. Provide a JSON array of steps to add."

    # Parse the new steps
    try:
        new_steps_input = json.loads(steps_json)
        if not isinstance(new_steps_input, list) or not new_steps_input:
            return "Error: steps_json must be a non-empty JSON array."
    except json.JSONDecodeError:
        return "Error: steps_json must be a valid JSON string."

    proj = _encode_project(project_name)

    # Fetch existing steps
    existing_detailed = []
    steps_result = _request(f"{proj}/testCases/{tc_id}/steps", token)
    if not steps_result.get("error") and steps_result.get("data"):
        steps_data = steps_result["data"].get("stepsData", {})
        existing_detailed = steps_data.get("detailed", [])

    # Build new step entries, numbering continues from existing
    start_number = len(existing_detailed) + 1
    new_detailed = []
    for i, s in enumerate(new_steps_input, start=start_number):
        step_rows = []
        if s.get("expectedResult"):
            step_rows.append({
                "type": "expectedResult",
                "expectedResult": {
                    "text": s["expectedResult"],
                    "fileReferences": [],
                },
            })
        new_detailed.append({
            "type": "step",
            "step": {
                "number": i,
                "text": s.get("text", ""),
                "stepRows": step_rows,
            },
        })

    # Combine existing + new
    combined = existing_detailed + new_detailed
    payload = {
        "stepsData": {
            "type": "detailed",
            "modifiedToCorrectSyntax": False,
            "basic": [],
            "detailed": combined,
        }
    }

    err = _put_steps(project_name, token, tc_id, payload)
    if err:
        return err

    return json.dumps({
        "message": f"Added {len(new_detailed)} step(s) to test case {test_case_identifier}",
        "test_case_id": tc_id,
        "previous_step_count": len(existing_detailed),
        "new_total_step_count": len(combined),
    }, indent=2)


@mcp.tool()
def link_test_case_to_requirement(
    project_name: str = "",
    test_case_identifier: str = "",
    requirement_identifier: str = "",
    link_type: str = "Requirement Tested By",
) -> str:
    """Link a test case to an existing requirement in Helix ALM.

    Uses a parentChildren link where the requirement is the parent and the test case
    is the child, matching the standard 'Requirement Tested By' traceability link.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        test_case_identifier: The test case tag (e.g. 'TC-42') or numeric ID.
        requirement_identifier: The requirement tag (e.g. 'BR-1960') or numeric ID.
        link_type: The link definition name to use. Defaults to 'Requirement Tested By',
                   the standard traceability link. Use 'Related Items' for a peer link.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    tc_id = _resolve_test_case_id(project_name, token, test_case_identifier)
    if tc_id is None:
        return f"Error: Could not find test case '{test_case_identifier}'."

    req_id = _resolve_requirement_id(project_name, token, requirement_identifier)
    if req_id is None:
        return f"Error: Could not find requirement '{requirement_identifier}'."

    proj = _encode_project(project_name)
    body = {
        "linksData": [
            {
                "linkDefinition": {"name": link_type},
                "type": "parentChildren",
                "parentChildren": {
                    "parent": {
                        "itemID": req_id,
                        "itemType": "requirements",
                    },
                    "children": [
                        {
                            "itemID": tc_id,
                            "itemType": "testCases",
                        }
                    ],
                },
            }
        ]
    }
    result = _request(f"{proj}/testCases/{tc_id}/links", token, body, "POST")
    if result.get("error"):
        return _friendly_error(result, "link the test case to the requirement")

    return json.dumps({
        "message": f"Test case {test_case_identifier} linked to requirement {requirement_identifier}",
        "test_case_id": tc_id,
        "requirement_id": req_id,
        "link_type": link_type,
    }, indent=2)


@mcp.tool()
def list_test_cases(project_name: str = "", columns: str = "", filter_name: str = "") -> str:
    """List test cases in a Helix ALM project.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        columns: Comma-separated field labels to include (e.g. "Summary,Priority"). Leave empty for defaults.
        filter_name: Optional saved filter name to apply.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    proj = _encode_project(project_name)
    params = []
    if columns:
        params.append(f"columns={urllib.parse.quote(columns)}")
    if filter_name:
        params.append(f"filter={urllib.parse.quote(filter_name)}")
    qs = ("?" + "&".join(params)) if params else ""

    result = _request(f"{proj}/testCases{qs}", token)
    if result.get("error"):
        return _friendly_error(result, "list test cases")

    tcs = result["data"].get("testCases", [])
    formatted = [_format_test_case(tc) for tc in tcs]
    return json.dumps({"count": len(formatted), "test_cases": formatted}, indent=2)


@mcp.tool()
def search_test_cases(project_name: str = "", search_text: str = "") -> str:
    """Search test cases by text across Summary and Description fields.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        search_text: Text to search for in test cases.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    proj = _encode_project(project_name)
    qs = f"?searchText={urllib.parse.quote(search_text)}"
    result = _request(f"{proj}/testCases{qs}", token)
    if result.get("error"):
        return _friendly_error(result, "search test cases")

    tcs = result["data"].get("testCases", [])
    formatted = [_format_test_case(tc) for tc in tcs]
    return json.dumps({"count": len(formatted), "test_cases": formatted}, indent=2)


@mcp.tool()
def get_test_case_types(project_name: str = "") -> str:
    """Get the test case types currently in use in a project (e.g. Validation, Functional,
    Regression). Note: this discovers types from existing test cases, so types that haven't
    been used yet may not appear.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    proj = _encode_project(project_name)
    # Request only the Type column to minimize payload
    result = _request(f"{proj}/testCases?columns={urllib.parse.quote('Type')}", token)
    if result.get("error"):
        return _friendly_error(result, "get test case types")

    types_seen = set()
    for tc in result["data"].get("testCases", []):
        val = _get_field(tc.get("fields", []), "Type")
        if val:
            types_seen.add(val)

    return json.dumps({
        "test_case_types": sorted(types_seen),
    }, indent=2)


# --- Document Tree helpers ---

def _resolve_document_id(project_name: str, token: str, identifier: str) -> int | None:
    """Resolve a document tag (e.g. 'RD-91') or numeric ID to the internal API id.

    Also supports matching by document name (case-insensitive).
    """
    proj = _encode_project(project_name)
    result = _request(f"{proj}/documents", token)
    if result.get("error"):
        return None

    for doc in result["data"].get("documents", []):
        # Match by tag
        if doc.get("tag", "").upper() == identifier.upper():
            return doc["id"]
        # Match by internal ID
        if str(doc.get("id", "")) == identifier:
            return doc["id"]
        # Match by just the number part (e.g. "91" matches "RD-91")
        tag = doc.get("tag", "")
        if "-" in tag and tag.split("-", 1)[1] == identifier:
            return doc["id"]
        # Match by document name (case-insensitive)
        fields = doc.get("fields", [])
        doc_name = _get_field(fields, "Name") or ""
        if doc_name and doc_name.lower() == identifier.lower():
            return doc["id"]

    return None


def _resolve_suite_id(project_name: str, token: str, identifier: str) -> int | None:
    """Resolve an automation suite name or numeric ID to the internal API id.

    Accepts a suite name (e.g. 'Regression Suite') or a numeric ID (e.g. '7').
    Name matching is case-insensitive.
    """
    # If purely numeric, try it as a direct ID
    if identifier.isdigit():
        return int(identifier)

    proj = _encode_project(project_name)
    result = _request(f"{proj}/automationSuites", token)
    if result.get("error"):
        return None

    suites_data = result["data"].get("automationSuitesData",
                                     result["data"].get("automationSuites", []))
    for suite in suites_data:
        if suite.get("name", "").lower() == identifier.lower():
            return suite["id"]

    return None


def _build_tree_recursive(proj: str, token: str, tree_id: int,
                          node_id: int | None, depth: int, max_depth: int) -> list:
    """Recursively build a tree of nodes with requirement details."""
    if depth > max_depth:
        return []

    if node_id is None:
        path = f"{proj}/documentTrees/{tree_id}/nodes"
        key = "nodesData"
    else:
        path = f"{proj}/documentTrees/{tree_id}/nodes/{node_id}/childNodes"
        key = "childNodesData"

    result = _request(path, token)
    if result.get("error"):
        return []

    nodes = []
    for node in result["data"].get(key, []):
        req_id = node.get("requirementID")
        entry = {
            "node_id": node["id"],
            "outline": node.get("outlineNumber", ""),
            "tag": node.get("tag", ""),
            "requirement_id": req_id,
        }

        # Fetch requirement summary
        req_result = _request(f"{proj}/requirements/{req_id}", token)
        if not req_result.get("error"):
            req = req_result["data"]
            fields = req.get("fields", [])
            entry["summary"] = _get_field(fields, "Summary") or ""
            entry["type"] = req.get("requirementType", {}).get("label", "")
            entry["priority"] = _get_field(fields, "Priority") or ""
            entry["status"] = _get_field(fields, "Status") or ""
            desc = _get_field(fields, "Description") or ""
            desc_clean = re.sub(r"<[^>]+>", " ", desc).strip()
            desc_clean = re.sub(r"\s+", " ", desc_clean)
            entry["description"] = desc_clean[:300]

        # Recurse into children
        children = _build_tree_recursive(proj, token, tree_id, node["id"],
                                         depth + 1, max_depth)
        if children:
            entry["children"] = children

        nodes.append(entry)

    return nodes


# --- Document Tree MCP Tools ---

@mcp.tool()
def list_documents(project_name: str = "") -> str:
    """List all requirement documents in a Helix ALM project.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    proj = _encode_project(project_name)
    result = _request(f"{proj}/documents", token)
    if result.get("error"):
        return _friendly_error(result, "list documents")

    docs = []
    for doc in result["data"].get("documents", []):
        fields = doc.get("fields", [])
        docs.append({
            "id": doc.get("id"),
            "tag": doc.get("tag", ""),
            "name": _get_field(fields, "Name") or "",
            "status": _get_field(fields, "Status") or "",
        })

    return json.dumps({"count": len(docs), "documents": docs}, indent=2)


@mcp.tool()
def create_document(project_name: str = "", name: str = "") -> str:
    """Create a new requirement document in a Helix ALM project.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        name: Name for the new requirement document.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    proj = _encode_project(project_name)
    body = {
        "documents": [
            {
                "fields": [
                    {
                        "label": "Name",
                        "type": "string",
                        "string": name,
                    }
                ]
            }
        ]
    }
    result = _request(f"{proj}/documents", token, body, "POST")

    if result.get("error"):
        return _friendly_error(result, "create the document")

    data = result.get("data", {})
    docs = data.get("documents", [])
    if docs:
        doc = docs[0]
        fields = doc.get("fields", [])
        return json.dumps({
            "message": "Document created successfully",
            "document": {
                "id": doc.get("id"),
                "tag": doc.get("tag", ""),
                "name": _get_field(fields, "Name") or name,
            }
        }, indent=2)

    return json.dumps({"message": "Document created", "response": data}, indent=2)


@mcp.tool()
def get_document_tree(project_name: str = "", document_identifier: str = "",
                      max_depth: int = 3) -> str:
    """Get the hierarchical tree structure of a requirement document, showing all
    sections and requirements organized in their outline order.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        document_identifier: Document tag (e.g. 'RD-91') or numeric ID.
        max_depth: Maximum depth of tree to retrieve (default 3). Use higher values for deeply nested documents.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    doc_id = _resolve_document_id(project_name, token, document_identifier)
    if doc_id is None:
        return f"Error: Could not find document '{document_identifier}'."

    proj = _encode_project(project_name)

    # Get document metadata
    doc_result = _request(f"{proj}/documents/{doc_id}", token)
    doc_name = ""
    if not doc_result.get("error"):
        doc_name = _get_field(doc_result["data"].get("fields", []), "Name") or ""

    # Build the tree recursively
    tree = _build_tree_recursive(proj, token, doc_id, None, 0, max_depth)

    return json.dumps({
        "document_id": doc_id,
        "document_name": doc_name,
        "tree": tree,
    }, indent=2)


@mcp.tool()
def get_document_node_children(project_name: str = "", document_identifier: str = "",
                               node_id: int = 0) -> str:
    """Get the direct child nodes of a specific node in a document tree.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        document_identifier: Document tag (e.g. 'RD-91') or numeric ID.
        node_id: The node ID to get children for (from get_document_tree results).
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    doc_id = _resolve_document_id(project_name, token, document_identifier)
    if doc_id is None:
        return f"Error: Could not find document '{document_identifier}'."

    proj = _encode_project(project_name)
    result = _request(f"{proj}/documentTrees/{doc_id}/nodes/{node_id}/childNodes", token)
    if result.get("error"):
        return _friendly_error(result, "get document node children")

    children = []
    for node in result["data"].get("childNodesData", []):
        req_id = node.get("requirementID")
        entry = {
            "node_id": node["id"],
            "outline": node.get("outlineNumber", ""),
            "tag": node.get("tag", ""),
            "requirement_id": req_id,
        }
        req_result = _request(f"{proj}/requirements/{req_id}", token)
        if not req_result.get("error"):
            req = req_result["data"]
            fields = req.get("fields", [])
            entry["summary"] = _get_field(fields, "Summary") or ""
            entry["type"] = req.get("requirementType", {}).get("label", "")
        children.append(entry)

    return json.dumps({"node_id": node_id, "children": children}, indent=2)


@mcp.tool()
def add_to_document_tree(project_name: str = "", document_identifier: str = "",
                         parent_node_id: int = 0,
                         requirement_identifiers: str = "") -> str:
    """Add one or more requirements as child nodes under a parent node in a document tree.

    IMPORTANT: You MUST call create_document_snapshot for this document BEFORE
    making this change so a point-in-time baseline exists.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        document_identifier: Document tag (e.g. 'RD-91') or numeric ID.
        parent_node_id: The node ID to add children under (from get_document_tree results).
        requirement_identifiers: Comma-separated requirement tags or IDs to add (e.g. 'FR-2219,NFR-2220,CR-2222').
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    doc_id = _resolve_document_id(project_name, token, document_identifier)
    if doc_id is None:
        return f"Error: Could not find document '{document_identifier}'."

    proj = _encode_project(project_name)

    # Resolve all requirement identifiers to internal IDs
    identifiers = [s.strip() for s in requirement_identifiers.split(",") if s.strip()]
    child_nodes = []
    resolve_errors = []
    for ident in identifiers:
        req_id = _resolve_requirement_id(project_name, token, ident)
        if req_id is None:
            resolve_errors.append(ident)
        else:
            child_nodes.append({"requirementID": req_id})

    if resolve_errors:
        return json.dumps({
            "error": f"Could not resolve requirements: {', '.join(resolve_errors)}",
        })

    if not child_nodes:
        return json.dumps({"error": "No valid requirements to add."})

    body = {"childNodesData": child_nodes}
    result = _request(
        f"{proj}/documentTrees/{doc_id}/nodes/{parent_node_id}/childNodes",
        token, body, "POST",
    )

    if result.get("error") and result["status"] != 206:
        return _friendly_error(result, "add requirements to the document tree")

    data = result.get("data", {})
    added = data.get("childNodesData", [])
    errors = data.get("errors", [])

    added_info = []
    for node in added:
        added_info.append({
            "node_id": node["id"],
            "outline": node.get("outlineNumber", ""),
            "tag": node.get("tag", ""),
        })

    response = {
        "message": f"Added {len(added)} requirement(s) to document tree",
        "added": added_info,
    }
    if errors:
        response["errors"] = errors

    return json.dumps(response, indent=2)


@mcp.tool()
def add_to_document_tree_top_level(project_name: str = "", document_identifier: str = "",
                                   requirement_identifiers: str = "") -> str:
    """Add one or more requirements as top-level nodes in a document tree.

    IMPORTANT: You MUST call create_document_snapshot for this document BEFORE
    making this change so a point-in-time baseline exists.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        document_identifier: Document tag (e.g. 'RD-91') or numeric ID.
        requirement_identifiers: Comma-separated requirement tags or IDs to add (e.g. 'OV-2246,OV-2247').
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    doc_id = _resolve_document_id(project_name, token, document_identifier)
    if doc_id is None:
        return f"Error: Could not find document '{document_identifier}'."

    proj = _encode_project(project_name)

    identifiers = [s.strip() for s in requirement_identifiers.split(",") if s.strip()]
    nodes_data = []
    resolve_errors = []
    for ident in identifiers:
        req_id = _resolve_requirement_id(project_name, token, ident)
        if req_id is None:
            resolve_errors.append(ident)
        else:
            nodes_data.append({"requirementID": req_id})

    if resolve_errors:
        return json.dumps({
            "error": f"Could not resolve requirements: {', '.join(resolve_errors)}",
        })

    if not nodes_data:
        return json.dumps({"error": "No valid requirements to add."})

    body = {"nodesData": nodes_data}
    result = _request(f"{proj}/documentTrees/{doc_id}/nodes", token, body, "POST")

    if result.get("error") and result["status"] != 206:
        return _friendly_error(result, "add requirements to the document tree")

    data = result.get("data", {})
    added = data.get("nodesData", [])
    errors = data.get("errors", [])

    added_info = []
    for node in added:
        added_info.append({
            "node_id": node["id"],
            "outline": node.get("outlineNumber", ""),
            "tag": node.get("tag", ""),
        })

    response = {
        "message": f"Added {len(added)} requirement(s) as top-level nodes",
        "added": added_info,
    }
    if errors:
        response["errors"] = errors

    return json.dumps(response, indent=2)


@mcp.tool()
def get_document_requirements(project_name: str = "", document_name: str = "") -> str:
    """Get all requirements that belong to a specific requirement document, grouped by type.
    This finds requirements via their 'Document List' field rather than the tree structure.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        document_name: The document name (or partial name) to match in the Document List field.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    proj = _encode_project(project_name)
    result = _request(f"{proj}/requirements", token)
    if result.get("error"):
        return _friendly_error(result, "get document requirements")

    matched = []
    for req in result["data"].get("requirements", []):
        fields = req.get("fields", [])
        doc_list = _get_field(fields, "Document List") or ""
        if document_name.lower() in doc_list.lower():
            desc = _get_field(fields, "Description") or ""
            desc_clean = re.sub(r"<[^>]+>", " ", desc).strip()
            desc_clean = re.sub(r"\s+", " ", desc_clean)
            matched.append({
                "id": req.get("id"),
                "tag": req.get("tag", ""),
                "type": req.get("requirementType", {}).get("label", ""),
                "summary": _get_field(fields, "Summary") or "",
                "priority": _get_field(fields, "Priority") or "",
                "status": _get_field(fields, "Status") or "",
                "description": desc_clean[:300],
            })

    # Group by type
    by_type = {}
    for r in matched:
        rtype = r["type"] or "Unknown"
        by_type.setdefault(rtype, []).append(r)

    return json.dumps({
        "document_name": document_name,
        "total_requirements": len(matched),
        "by_type": by_type,
    }, indent=2)


@mcp.tool()
def create_document_snapshot(
    project_name: str = "",
    document_identifier: str = "",
    name: str = "",
    description: str = "",
) -> str:
    """Create a snapshot of a requirement document in Helix ALM.

    Use this to capture a point-in-time snapshot of a document before making
    changes to its requirements. This preserves the current state so it can
    be referenced or compared later.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        document_identifier: Document tag (e.g. 'RD-91'), numeric ID, or document name.
        name: Label for the snapshot (e.g. '2026-04-16' or 'Pre-release baseline').
        description: Optional comment explaining the purpose of the snapshot.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    doc_id = _resolve_document_id(project_name, token, document_identifier)
    if doc_id is None:
        return f"Error: Could not find document '{document_identifier}'."

    if not name:
        return "Error: A label for the snapshot is required."

    proj = _encode_project(project_name)
    snapshot_entry = {"label": name}
    if description:
        snapshot_entry["comment"] = description
    body = {"snapshotsData": [snapshot_entry]}

    result = _request(f"{proj}/documents/{doc_id}/snapshots", token, body, "POST")
    if result.get("error"):
        return _friendly_error(result, "create document snapshot")

    data = result.get("data", {})
    return json.dumps({
        "message": f"Snapshot '{name}' created for document '{document_identifier}'",
        "document_id": doc_id,
        "snapshot": data,
    }, indent=2)


# --- Automation Suite MCP Tools ---

@mcp.tool()
def list_automation_suites(project_name: str = "") -> str:
    """List all automation suites in a Helix ALM project.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    proj = _encode_project(project_name)
    result = _request(f"{proj}/automationSuites", token)
    if result.get("error"):
        return _friendly_error(result, "list automation suites")

    suites = []
    suites_data = result["data"].get("automationSuitesData", result["data"].get("automationSuites", []))
    for suite in suites_data:
        suites.append({
            "id": suite.get("id"),
            "name": suite.get("name", ""),
            "description": suite.get("description", ""),
            "active": suite.get("active"),
        })

    return json.dumps({"count": len(suites), "automation_suites": suites}, indent=2)


@mcp.tool()
def create_automation_suite(project_name: str = "", name: str = "",
                            description: str = "") -> str:
    """Create a new automation suite in a Helix ALM project.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        name: Name of the automation suite.
        description: Optional description.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    proj = _encode_project(project_name)
    body = {
        "automationSuitesData": [{
            "name": name,
            "description": description,
            "active": True,
        }]
    }
    result = _request(f"{proj}/automationSuites", token, body, "POST")
    if result.get("error"):
        return _friendly_error(result, "create the automation suite")

    created = result["data"].get("automationSuitesData", [])
    if created:
        suite = created[0]
        return json.dumps({
            "message": f"Automation suite '{name}' created successfully",
            "suite": {
                "id": suite.get("id"),
                "name": suite.get("name"),
                "description": suite.get("description"),
                "active": suite.get("active"),
            },
        }, indent=2)

    return json.dumps({"message": "Suite created", "data": result["data"]}, indent=2)


@mcp.tool()
def get_automation_suite(project_name: str = "", suite_identifier: str = "") -> str:
    """Get details of a specific automation suite.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        suite_identifier: The automation suite name (e.g. 'Regression Suite') or numeric ID.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    suite_id = _resolve_suite_id(project_name, token, suite_identifier)
    if suite_id is None:
        return f"Could not find automation suite '{suite_identifier}'. Use list_automation_suites to see available suites."

    proj = _encode_project(project_name)
    result = _request(f"{proj}/automationSuites/{suite_id}", token)
    if result.get("error"):
        return _friendly_error(result, f"get automation suite '{suite_identifier}'")

    data = result["data"]
    suite = {
        "id": data.get("id"),
        "name": data.get("name", ""),
        "description": data.get("description", ""),
        "active": data.get("active"),
    }
    # Include test count if available
    if "testCount" in data:
        suite["test_count"] = data["testCount"]
    if "lastBuildNumber" in data:
        suite["last_build_number"] = data["lastBuildNumber"]

    return json.dumps({"automation_suite": suite}, indent=2)


@mcp.tool()
def submit_automation_build(
    project_name: str = "",
    suite_identifier: str = "",
    build_number: str = "",
    results_json: str = "",
    description: str = "",
    branch: str = "",
    start_date: str = "",
    duration: int = 0,
    test_run_set: str = "",
    external_url: str = "",
    properties_json: str = "",
) -> str:
    """Submit automated test results to a Helix ALM automation suite.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        suite_identifier: The automation suite name (e.g. 'Regression Suite') or numeric ID.
        build_number: Build number/identifier (e.g. '167', 'v2.1.0-rc1').
        results_json: JSON array of test results. Each result must have 'name' and 'status' (with 'label' being 'passed', 'failed', 'skipped', etc.). Example: '[{"name": "test_login", "uniqueName": "com.example.tests.test_login", "status": {"label": "passed"}, "duration": 1500}]'
        description: Optional build description.
        branch: Optional branch name (e.g. 'main', 'Mainline Branch').
        start_date: Optional build start date in ISO 8601 format (e.g. '2026-03-26T11:25:46Z'). Defaults to now.
        duration: Optional total build duration in milliseconds.
        test_run_set: Optional test run set label to associate results with.
        external_url: Optional URL to external CI system (e.g. Jenkins build URL).
        properties_json: Optional JSON array of name/value property pairs, e.g. '[{"name": "os", "value": "linux"}]'.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    suite_id = _resolve_suite_id(project_name, token, suite_identifier)
    if suite_id is None:
        return f"Could not find automation suite '{suite_identifier}'. Use list_automation_suites to see available suites."

    # Parse the results JSON
    try:
        results = json.loads(results_json)
    except json.JSONDecodeError as e:
        return f"Error: results_json is not valid JSON: {e}"

    if not isinstance(results, list) or not results:
        return "Error: results_json must be a non-empty JSON array of test results."

    # Normalize result entries — ensure status has proper structure
    for r in results:
        if "status" in r and isinstance(r["status"], str):
            r["status"] = {"label": r["status"]}

    # Build the request body
    body: dict = {
        "number": build_number,
        "results": results,
    }

    if description:
        body["description"] = description
    if branch:
        body["branch"] = branch
    if start_date:
        body["startDate"] = start_date
    if duration:
        body["duration"] = duration
    if test_run_set:
        body["testRunSet"] = {"label": test_run_set}
    if external_url:
        body["externalURL"] = external_url

    if properties_json:
        try:
            props = json.loads(properties_json)
            if isinstance(props, list):
                body["properties"] = props
        except json.JSONDecodeError:
            return "Error: properties_json is not valid JSON."

    proj = _encode_project(project_name)
    result = _request(f"{proj}/automationSuites/{suite_id}/submitBuild", token, body, "POST")

    if result.get("error"):
        return _friendly_error(result, "submit the build results")

    return json.dumps({
        "message": f"Build {build_number} submitted to automation suite '{suite_identifier}'",
        "data": result.get("data"),
    }, indent=2)


@mcp.tool()
def submit_automation_results_simple(
    project_name: str = "",
    suite_identifier: str = "",
    build_number: str = "",
    test_names_passed: str = "",
    test_names_failed: str = "",
    test_names_skipped: str = "",
    description: str = "",
    branch: str = "",
    external_url: str = "",
) -> str:
    """Submit automated test results using simple comma-separated test name lists.
    This is a simplified alternative to submit_automation_build for quick submissions.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        suite_identifier: The automation suite name (e.g. 'Regression Suite') or numeric ID.
        build_number: Build number/identifier (e.g. '167').
        test_names_passed: Comma-separated names of tests that passed.
        test_names_failed: Comma-separated names of tests that failed.
        test_names_skipped: Comma-separated names of tests that were skipped.
        description: Optional build description.
        branch: Optional branch name.
        external_url: Optional URL to external CI system.
    """
    results = []
    for name in (n.strip() for n in test_names_passed.split(",") if n.strip()):
        results.append({"name": name, "uniqueName": name, "status": {"label": "passed"}})
    for name in (n.strip() for n in test_names_failed.split(",") if n.strip()):
        results.append({"name": name, "uniqueName": name, "status": {"label": "failed"}})
    for name in (n.strip() for n in test_names_skipped.split(",") if n.strip()):
        results.append({"name": name, "uniqueName": name, "status": {"label": "skipped"}})

    if not results:
        return "Error: At least one test name must be provided."

    return submit_automation_build(
        project_name=project_name,
        suite_identifier=suite_identifier,
        build_number=build_number,
        results_json=json.dumps(results),
        description=description,
        branch=branch,
        external_url=external_url,
    )


@mcp.tool()
def list_automation_builds(project_name: str = "", suite_identifier: str = "") -> str:
    """List builds that have been submitted to an automation suite.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        suite_identifier: The automation suite name (e.g. 'Regression Suite') or numeric ID.
    """
    project_name = _resolve_project(project_name)
    if not project_name:
        return _no_project_msg()
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    suite_id = _resolve_suite_id(project_name, token, suite_identifier)
    if suite_id is None:
        return f"Could not find automation suite '{suite_identifier}'. Use list_automation_suites to see available suites."

    proj = _encode_project(project_name)
    result = _request(f"{proj}/automationSuites/{suite_id}/builds", token)
    if result.get("error"):
        return _friendly_error(result, "list automation builds")

    data = result["data"]
    builds_list = data.get("buildsData", data.get("builds", []))
    if isinstance(builds_list, list):
        formatted_builds = []
        for build in builds_list:
            entry = {
                "id": build.get("id"),
                "number": build.get("number", ""),
                "description": build.get("description", ""),
                "branch": build.get("branch", ""),
                "start_date": build.get("startDate", ""),
            }
            # Include result summary if available
            if "resultSummary" in build:
                entry["result_summary"] = build["resultSummary"]
            elif "results" in build:
                results = build["results"]
                if isinstance(results, list):
                    entry["result_count"] = len(results)
            formatted_builds.append(entry)
        return json.dumps({
            "suite": suite_identifier,
            "count": len(formatted_builds),
            "builds": formatted_builds,
        }, indent=2)

    return json.dumps(data, indent=2)


# --- JUnit / xUnit XML Parsing ---

def _parse_junit_xml(xml_content: str) -> dict:
    """Parse JUnit XML format into a normalized structure.

    Supports both single <testsuite> and wrapped <testsuites> formats.
    """
    root = ET.fromstring(xml_content)

    suites = []
    if root.tag == "testsuites":
        suites = list(root.iter("testsuite"))
    elif root.tag == "testsuite":
        suites = [root]
    else:
        raise ValueError(f"Unexpected root element: {root.tag}")

    results = []
    total_time = 0.0

    for suite in suites:
        suite_name = suite.get("name", "")
        for tc in suite.findall("testcase"):
            name = tc.get("name", "")
            classname = tc.get("classname", "")
            unique_name = f"{classname}.{name}" if classname else name
            duration_ms = int(float(tc.get("time", "0")) * 1000)
            total_time += float(tc.get("time", "0"))

            # Determine status from child elements
            failure = tc.find("failure")
            error = tc.find("error")
            skipped = tc.find("skipped")

            if failure is not None:
                status = "failed"
                message = failure.get("message", "")
                detail = failure.text or ""
            elif error is not None:
                status = "failed"
                message = error.get("message", "")
                detail = error.text or ""
            elif skipped is not None:
                status = "skipped"
                message = skipped.get("message", "")
                detail = ""
            else:
                status = "passed"
                message = ""
                detail = ""

            entry = {
                "name": name,
                "uniqueName": unique_name,
                "status": {"label": status},
                "duration": duration_ms,
            }

            if message or detail:
                props = []
                if message:
                    props.append({"name": "message", "value": message})
                if detail.strip():
                    props.append({"name": "detail", "value": detail.strip()[:500]})
                entry["properties"] = props

            results.append(entry)

    suite_names = [s.get("name", "") for s in suites if s.get("name")]
    description = f"JUnit results from: {', '.join(suite_names)}" if suite_names else "JUnit results"

    return {
        "results": results,
        "description": description,
        "duration": int(total_time * 1000),
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r["status"]["label"] == "passed"),
            "failed": sum(1 for r in results if r["status"]["label"] == "failed"),
            "skipped": sum(1 for r in results if r["status"]["label"] == "skipped"),
        },
    }


def _parse_xunit_xml(xml_content: str) -> dict:
    """Parse xUnit v2 XML format into a normalized structure.

    Supports both <assemblies>/<assembly> wrapper and single <assembly>.
    """
    root = ET.fromstring(xml_content)

    assemblies = []
    if root.tag == "assemblies":
        assemblies = list(root.iter("assembly"))
    elif root.tag == "assembly":
        assemblies = [root]
    else:
        raise ValueError(f"Unexpected root element: {root.tag}")

    results = []
    total_time = 0.0

    for assembly in assemblies:
        for collection in assembly.iter("collection"):
            for test in collection.findall("test"):
                name = test.get("method", test.get("name", ""))
                type_name = test.get("type", "")
                full_name = test.get("name", "")
                unique_name = full_name if full_name else (f"{type_name}.{name}" if type_name else name)
                duration_ms = int(float(test.get("time", "0")) * 1000)
                total_time += float(test.get("time", "0"))

                result_attr = test.get("result", "").lower()
                if result_attr == "pass":
                    status = "passed"
                elif result_attr == "fail":
                    status = "failed"
                elif result_attr == "skip":
                    status = "skipped"
                else:
                    status = result_attr or "unknown"

                entry = {
                    "name": name,
                    "uniqueName": unique_name,
                    "status": {"label": status},
                    "duration": duration_ms,
                }

                # Extract failure info
                failure_el = test.find("failure")
                if failure_el is not None:
                    msg_el = failure_el.find("message")
                    stack_el = failure_el.find("stack-trace")
                    props = []
                    if msg_el is not None and msg_el.text:
                        props.append({"name": "message", "value": msg_el.text.strip()[:500]})
                    if stack_el is not None and stack_el.text:
                        props.append({"name": "stackTrace", "value": stack_el.text.strip()[:500]})
                    if props:
                        entry["properties"] = props

                # Extract skip reason
                reason_el = test.find("reason")
                if reason_el is not None and reason_el.text:
                    entry.setdefault("properties", []).append(
                        {"name": "skipReason", "value": reason_el.text.strip()[:500]}
                    )

                results.append(entry)

    assembly_names = [a.get("name", "") for a in assemblies if a.get("name")]
    description = f"xUnit results from: {', '.join(assembly_names)}" if assembly_names else "xUnit results"

    return {
        "results": results,
        "description": description,
        "duration": int(total_time * 1000),
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r["status"]["label"] == "passed"),
            "failed": sum(1 for r in results if r["status"]["label"] == "failed"),
            "skipped": sum(1 for r in results if r["status"]["label"] == "skipped"),
        },
    }


def _detect_and_parse_xml(xml_content: str) -> dict:
    """Auto-detect whether XML is JUnit or xUnit format and parse accordingly."""
    root = ET.fromstring(xml_content)

    if root.tag in ("testsuites", "testsuite"):
        return _parse_junit_xml(xml_content)
    elif root.tag in ("assemblies", "assembly"):
        return _parse_xunit_xml(xml_content)
    else:
        raise ValueError(
            f"Unrecognized XML format (root element: '{root.tag}'). "
            "Expected JUnit (<testsuites> or <testsuite>) or xUnit (<assemblies> or <assembly>)."
        )


# --- JUnit / xUnit MCP Tools ---

@mcp.tool()
def submit_junit_results(
    project_name: str = "",
    suite_identifier: str = "",
    build_number: str = "",
    file_path: str = "",
    branch: str = "",
    external_url: str = "",
    test_run_set: str = "",
) -> str:
    """Submit test results from a JUnit XML file to a Helix ALM automation suite.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        suite_identifier: The automation suite name (e.g. 'Regression Suite') or numeric ID.
        build_number: Build number/identifier (e.g. '167').
        file_path: Absolute path to the JUnit XML results file.
        branch: Optional branch name (e.g. 'main').
        external_url: Optional URL to external CI system.
        test_run_set: Optional test run set label.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            xml_content = f.read()
    except FileNotFoundError:
        return f"Error: File not found: {file_path}"
    except Exception as e:
        return f"Error reading file: {e}"

    try:
        parsed = _parse_junit_xml(xml_content)
    except Exception as e:
        return f"Error parsing JUnit XML: {e}"

    result = submit_automation_build(
        project_name=project_name,
        suite_identifier=suite_identifier,
        build_number=build_number,
        results_json=json.dumps(parsed["results"]),
        description=parsed["description"],
        branch=branch,
        duration=parsed["duration"],
        external_url=external_url,
        test_run_set=test_run_set,
    )

    # Enrich the response with the parsed summary
    try:
        response = json.loads(result)
        response["parsed_summary"] = parsed["summary"]
        return json.dumps(response, indent=2)
    except json.JSONDecodeError:
        return result


@mcp.tool()
def submit_xunit_results(
    project_name: str = "",
    suite_identifier: str = "",
    build_number: str = "",
    file_path: str = "",
    branch: str = "",
    external_url: str = "",
    test_run_set: str = "",
) -> str:
    """Submit test results from an xUnit v2 XML file to a Helix ALM automation suite.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        suite_identifier: The automation suite name (e.g. 'Regression Suite') or numeric ID.
        build_number: Build number/identifier (e.g. '167').
        file_path: Absolute path to the xUnit XML results file.
        branch: Optional branch name (e.g. 'main').
        external_url: Optional URL to external CI system.
        test_run_set: Optional test run set label.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            xml_content = f.read()
    except FileNotFoundError:
        return f"Error: File not found: {file_path}"
    except Exception as e:
        return f"Error reading file: {e}"

    try:
        parsed = _parse_xunit_xml(xml_content)
    except Exception as e:
        return f"Error parsing xUnit XML: {e}"

    result = submit_automation_build(
        project_name=project_name,
        suite_identifier=suite_identifier,
        build_number=build_number,
        results_json=json.dumps(parsed["results"]),
        description=parsed["description"],
        branch=branch,
        duration=parsed["duration"],
        external_url=external_url,
        test_run_set=test_run_set,
    )

    try:
        response = json.loads(result)
        response["parsed_summary"] = parsed["summary"]
        return json.dumps(response, indent=2)
    except json.JSONDecodeError:
        return result


@mcp.tool()
def submit_test_results_xml(
    project_name: str = "",
    suite_identifier: str = "",
    build_number: str = "",
    file_path: str = "",
    branch: str = "",
    external_url: str = "",
    test_run_set: str = "",
) -> str:
    """Submit test results from a JUnit or xUnit XML file, auto-detecting the format.

    Args:
        project_name: Name of the Helix ALM project. Uses the default project if not specified.
        suite_identifier: The automation suite name (e.g. 'Regression Suite') or numeric ID.
        build_number: Build number/identifier (e.g. '167').
        file_path: Absolute path to the XML results file (JUnit or xUnit format).
        branch: Optional branch name (e.g. 'main').
        external_url: Optional URL to external CI system.
        test_run_set: Optional test run set label.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            xml_content = f.read()
    except FileNotFoundError:
        return f"Error: File not found: {file_path}"
    except Exception as e:
        return f"Error reading file: {e}"

    try:
        parsed = _detect_and_parse_xml(xml_content)
    except Exception as e:
        return f"Error parsing XML: {e}"

    result = submit_automation_build(
        project_name=project_name,
        suite_identifier=suite_identifier,
        build_number=build_number,
        results_json=json.dumps(parsed["results"]),
        description=parsed["description"],
        branch=branch,
        duration=parsed["duration"],
        external_url=external_url,
        test_run_set=test_run_set,
    )

    try:
        response = json.loads(result)
        response["parsed_summary"] = parsed["summary"]
        response["format_detected"] = "JUnit" if "JUnit" in parsed["description"] else "xUnit"
        return json.dumps(response, indent=2)
    except json.JSONDecodeError:
        return result


@mcp.tool()
def preview_test_results_xml(file_path: str) -> str:
    """Parse and preview a JUnit or xUnit XML file without submitting it.
    Useful to inspect what would be submitted before sending to Helix ALM.

    Args:
        file_path: Absolute path to the XML results file (JUnit or xUnit format).
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            xml_content = f.read()
    except FileNotFoundError:
        return f"Error: File not found: {file_path}"
    except Exception as e:
        return f"Error reading file: {e}"

    try:
        parsed = _detect_and_parse_xml(xml_content)
    except Exception as e:
        return f"Error parsing XML: {e}"

    fmt = "JUnit" if "JUnit" in parsed["description"] else "xUnit"

    # Build a readable preview
    preview = {
        "format": fmt,
        "description": parsed["description"],
        "duration_ms": parsed["duration"],
        "summary": parsed["summary"],
        "results": [],
    }

    for r in parsed["results"]:
        entry = {
            "name": r["name"],
            "uniqueName": r["uniqueName"],
            "status": r["status"]["label"],
            "duration_ms": r.get("duration", 0),
        }
        props = r.get("properties", [])
        for p in props:
            entry[p["name"]] = p["value"][:200]
        preview["results"].append(entry)

    return json.dumps(preview, indent=2)


# --- Azure DevOps helpers ---

def _azdo_configured() -> bool:
    """Check if Azure DevOps connection is configured for this session."""
    return bool(_session["azdo_org"] and _session["azdo_project"] and _session["azdo_pat"])


def _azdo_not_configured_msg() -> str:
    return (
        "Azure DevOps is not configured for this session. "
        "Please call the configure_azure_devops tool first with your organization, project, and PAT."
    )


def _azdo_request(url: str, org: str = "", pat: str = "") -> dict:
    """Send a GET request to the Azure DevOps REST API."""
    token = pat or _session["azdo_pat"]
    if not token:
        return {"error": True, "message": "Azure DevOps PAT not configured. Call configure_azure_devops first."}

    creds = base64.b64encode(f":{token}".encode()).decode()
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Basic {creds}")
    req.add_header("Accept", "application/json")

    try:
        resp = urllib.request.urlopen(req)
        data = resp.read()
        resp.close()
        if data:
            return {"status": resp.status, "data": json.loads(data.decode())}
        return {"status": resp.status, "data": None}
    except urllib.error.HTTPError as e:
        err_body = e.fp.read()
        try:
            err_data = json.loads(err_body.decode())
        except Exception:
            err_data = {"raw": err_body.decode()}
        return {"status": e.code, "data": err_data, "error": True}
    except Exception as e:
        return {"status": 0, "data": None, "error": True, "message": str(e)}


def _azdo_base_url(org: str = "", project: str = "") -> str:
    o = org or _session["azdo_org"]
    p = project or _session["azdo_project"]
    return f"https://dev.azure.com/{urllib.parse.quote(o)}/{urllib.parse.quote(p)}"


# --- Azure DevOps MCP Tools ---

@mcp.tool()
def azdo_list_pipelines(azdo_org: str = "", azdo_project: str = "",
                        azdo_pat: str = "") -> str:
    """List Azure DevOps build pipelines (definitions).

    Args:
        azdo_org: Azure DevOps organization name. Uses session config if empty.
        azdo_project: Azure DevOps project name. Uses session config if empty.
        azdo_pat: Personal access token. Uses session config if empty.
    """
    if not azdo_org and not azdo_pat and not _azdo_configured():
        return _azdo_not_configured_msg()
    base = _azdo_base_url(azdo_org, azdo_project)
    result = _azdo_request(f"{base}/_apis/build/definitions?api-version=7.1", azdo_org, azdo_pat)
    if result.get("error"):
        return _friendly_error(result, "list Azure DevOps pipelines")

    pipelines = []
    for d in result["data"].get("value", []):
        pipelines.append({
            "id": d.get("id"),
            "name": d.get("name", ""),
            "path": d.get("path", ""),
        })

    return json.dumps({"count": len(pipelines), "pipelines": pipelines}, indent=2)


@mcp.tool()
def azdo_list_builds(azdo_org: str = "", azdo_project: str = "",
                     azdo_pat: str = "", pipeline_id: int = 0,
                     top: int = 10) -> str:
    """List recent Azure DevOps builds, optionally filtered by pipeline.

    Args:
        azdo_org: Azure DevOps organization name. Uses session config if empty.
        azdo_project: Azure DevOps project name. Uses session config if empty.
        azdo_pat: Personal access token. Uses session config if empty.
        pipeline_id: Optional pipeline/definition ID to filter by. 0 means all pipelines.
        top: Number of builds to return (default 10).
    """
    if not azdo_org and not azdo_pat and not _azdo_configured():
        return _azdo_not_configured_msg()
    base = _azdo_base_url(azdo_org, azdo_project)
    params = f"$top={top}&api-version=7.1"
    if pipeline_id:
        params += f"&definitions={pipeline_id}"
    result = _azdo_request(f"{base}/_apis/build/builds?{params}", azdo_org, azdo_pat)
    if result.get("error"):
        return _friendly_error(result, "list Azure DevOps builds")

    builds = []
    for b in result["data"].get("value", []):
        builds.append({
            "id": b.get("id"),
            "buildNumber": b.get("buildNumber", ""),
            "status": b.get("status", ""),
            "result": b.get("result", ""),
            "sourceBranch": b.get("sourceBranch", ""),
            "startTime": b.get("startTime", ""),
            "finishTime": b.get("finishTime", ""),
            "definition": b.get("definition", {}).get("name", ""),
            "url": b.get("_links", {}).get("web", {}).get("href", ""),
        })

    return json.dumps({"count": len(builds), "builds": builds}, indent=2)


@mcp.tool()
def azdo_get_test_results(build_id: int, azdo_org: str = "",
                          azdo_project: str = "", azdo_pat: str = "") -> str:
    """Get test results from a specific Azure DevOps build.

    Args:
        build_id: The Azure DevOps build ID.
        azdo_org: Azure DevOps organization name. Uses session config if empty.
        azdo_project: Azure DevOps project name. Uses session config if empty.
        azdo_pat: Personal access token. Uses session config if empty.
    """
    if not azdo_org and not azdo_pat and not _azdo_configured():
        return _azdo_not_configured_msg()
    base = _azdo_base_url(azdo_org, azdo_project)
    pat = azdo_pat or _session["azdo_pat"]

    # Get test runs for this build
    runs_result = _azdo_request(
        f"{base}/_apis/test/runs?buildIds={build_id}&api-version=7.1", azdo_org, pat
    )
    if runs_result.get("error"):
        return _friendly_error(runs_result, "fetch test runs from Azure DevOps")

    runs = runs_result["data"].get("value", [])
    if not runs:
        return json.dumps({"message": f"No test runs found for build {build_id}.", "results": []})

    all_results = []
    for run in runs:
        run_id = run.get("id")
        run_name = run.get("name", "")

        # Get individual test results for this run
        results_resp = _azdo_request(
            f"{base}/_apis/test/runs/{run_id}/results?api-version=7.1", azdo_org, pat
        )
        if results_resp.get("error"):
            continue

        for tr in results_resp["data"].get("value", []):
            outcome = (tr.get("outcome") or "").lower()
            # Map Azure DevOps outcomes to standard labels
            if outcome == "passed":
                status = "passed"
            elif outcome in ("failed", "aborted", "error"):
                status = "failed"
            elif outcome in ("notexecuted", "notimpacted", "inconclusive"):
                status = "skipped"
            else:
                status = outcome or "unknown"

            entry = {
                "name": tr.get("testCaseTitle", tr.get("automatedTestName", "")),
                "uniqueName": tr.get("automatedTestName", tr.get("testCaseTitle", "")),
                "status": status,
                "duration_ms": tr.get("durationInMs", 0),
                "run_name": run_name,
                "error_message": tr.get("errorMessage", ""),
                "stack_trace": (tr.get("stackTrace") or "")[:500],
            }
            all_results.append(entry)

    summary = {
        "total": len(all_results),
        "passed": sum(1 for r in all_results if r["status"] == "passed"),
        "failed": sum(1 for r in all_results if r["status"] == "failed"),
        "skipped": sum(1 for r in all_results if r["status"] == "skipped"),
    }

    return json.dumps({
        "build_id": build_id,
        "test_runs": len(runs),
        "summary": summary,
        "results": all_results,
    }, indent=2)


@mcp.tool()
def azdo_submit_to_helix_alm(
    build_id: int = 0,
    helix_project_name: str = "",
    helix_suite_identifier: str = "",
    azdo_org: str = "",
    azdo_project: str = "",
    azdo_pat: str = "",
    build_number_override: str = "",
    branch_override: str = "",
) -> str:
    """Fetch test results from an Azure DevOps build and submit them to a Helix ALM automation suite.

    This is the main integration tool: it pulls test results from Azure DevOps
    and pushes them into Helix ALM in one step.

    Args:
        build_id: The Azure DevOps build ID to fetch results from.
        helix_project_name: Name of the Helix ALM project. Uses the default project if not specified.
        helix_suite_identifier: The Helix ALM automation suite name (e.g. 'Regression Suite') or numeric ID.
        azdo_org: Azure DevOps organization name. Uses session config if empty.
        azdo_project: Azure DevOps project name. Uses session config if empty.
        azdo_pat: Personal access token. Uses session config if empty.
        build_number_override: Override the build number (defaults to Azure DevOps buildNumber).
        branch_override: Override the branch name (defaults to Azure DevOps sourceBranch).
    """
    helix_project_name = _resolve_project(helix_project_name)
    if not helix_project_name:
        return _no_project_msg()
    if not azdo_org and not azdo_pat and not _azdo_configured():
        return _azdo_not_configured_msg()
    if not _helix_configured():
        return _helix_not_configured_msg()
    base = _azdo_base_url(azdo_org, azdo_project)
    pat = azdo_pat or _session["azdo_pat"]

    # Get build info for metadata
    build_result = _azdo_request(
        f"{base}/_apis/build/builds/{build_id}?api-version=7.1", azdo_org, pat
    )
    if build_result.get("error"):
        return _friendly_error(build_result, "fetch build info from Azure DevOps")

    build_data = build_result["data"]
    build_number = build_number_override or build_data.get("buildNumber", str(build_id))
    branch = branch_override or build_data.get("sourceBranch", "").replace("refs/heads/", "")
    build_url = build_data.get("_links", {}).get("web", {}).get("href", "")
    definition_name = build_data.get("definition", {}).get("name", "")

    # Calculate duration from start/finish times
    duration = 0
    start_time = build_data.get("startTime", "")
    finish_time = build_data.get("finishTime", "")

    # Get test results
    test_data_str = azdo_get_test_results(build_id, azdo_org, azdo_project, azdo_pat)
    test_data = json.loads(test_data_str)

    if not test_data.get("results"):
        return json.dumps({
            "error": f"No test results found for Azure DevOps build {build_id}.",
            "build_number": build_number,
        })

    # Convert to Helix ALM format
    helix_results = []
    for r in test_data["results"]:
        entry = {
            "name": r["name"],
            "uniqueName": r["uniqueName"],
            "status": {"label": r["status"]},
            "duration": r.get("duration_ms", 0),
        }

        # Add failure info as properties
        props = []
        if r.get("error_message"):
            props.append({"name": "errorMessage", "value": r["error_message"][:500]})
        if r.get("stack_trace"):
            props.append({"name": "stackTrace", "value": r["stack_trace"][:500]})
        if props:
            entry["properties"] = props

        helix_results.append(entry)

    description = f"Azure DevOps build {build_number} from pipeline '{definition_name}'"

    # Submit to Helix ALM
    submit_result = submit_automation_build(
        project_name=helix_project_name,
        suite_identifier=helix_suite_identifier,
        build_number=build_number,
        results_json=json.dumps(helix_results),
        description=description,
        branch=branch,
        external_url=build_url,
        start_date=start_time,
        duration=duration,
    )

    try:
        response = json.loads(submit_result)
        response["azure_devops"] = {
            "build_id": build_id,
            "build_number": build_number,
            "branch": branch,
            "pipeline": definition_name,
            "url": build_url,
        }
        response["test_summary"] = test_data["summary"]
        return json.dumps(response, indent=2)
    except json.JSONDecodeError:
        return submit_result


@mcp.tool()
def azdo_submit_latest_to_helix_alm(
    helix_project_name: str = "",
    helix_suite_identifier: str = "",
    pipeline_id: int = 0,
    azdo_org: str = "",
    azdo_project: str = "",
    azdo_pat: str = "",
) -> str:
    """Fetch test results from the latest completed Azure DevOps build and submit
    them to a Helix ALM automation suite. Optionally filter by pipeline.

    Args:
        helix_project_name: Name of the Helix ALM project. Uses the default project if not specified.
        helix_suite_identifier: The Helix ALM automation suite name (e.g. 'Regression Suite') or numeric ID.
        pipeline_id: Optional pipeline/definition ID to filter by. 0 means latest from any pipeline.
        azdo_org: Azure DevOps organization name. Uses session config if empty.
        azdo_project: Azure DevOps project name. Uses session config if empty.
        azdo_pat: Personal access token. Uses session config if empty.
    """
    helix_project_name = _resolve_project(helix_project_name)
    if not helix_project_name:
        return _no_project_msg()
    if not azdo_org and not azdo_pat and not _azdo_configured():
        return _azdo_not_configured_msg()
    if not _helix_configured():
        return _helix_not_configured_msg()
    base = _azdo_base_url(azdo_org, azdo_project)
    pat = azdo_pat or _session["azdo_pat"]

    # Get the latest completed build
    params = "$top=1&statusFilter=completed&api-version=7.1"
    if pipeline_id:
        params += f"&definitions={pipeline_id}"

    result = _azdo_request(f"{base}/_apis/build/builds?{params}", azdo_org, pat)
    if result.get("error"):
        return _friendly_error(result, "fetch builds from Azure DevOps")

    builds = result["data"].get("value", [])
    if not builds:
        return json.dumps({"error": "No completed builds found."})

    latest = builds[0]
    build_id = latest["id"]

    return azdo_submit_to_helix_alm(
        build_id=build_id,
        helix_project_name=helix_project_name,
        helix_suite_identifier=helix_suite_identifier,
        azdo_org=azdo_org,
        azdo_project=azdo_project,
        azdo_pat=azdo_pat,
    )


if __name__ == "__main__":
    mcp.run()
