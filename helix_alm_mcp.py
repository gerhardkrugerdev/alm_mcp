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


mcp = FastMCP(
    "Helix ALM Requirements",
    instructions="MCP server for managing requirements, documents, and automation suites in Helix ALM, "
    "with Azure DevOps integration. IMPORTANT: Before using any Helix ALM tools, users must first "
    "call configure_helix_alm with their server URL and API token. The user only needs to provide "
    "the server base URL (e.g. 'https://server:8443/') and their API token. For Azure DevOps "
    "integration, call configure_azure_devops with org, project, and PAT. Credentials are stored "
    "in memory only for the session and are never written to disk. If a tool returns a "
    "'not configured' error, prompt the user to call the appropriate configure tool.",
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


def _resolve_requirement_id(project_name: str, token: str, identifier: str) -> int | None:
    """Resolve a requirement tag (e.g. 'BR-1960') or numeric ID to the internal API id.

    Users typically reference requirements by tag, but the API uses internal IDs.
    """
    # If it's purely numeric, try it as a direct ID first
    if identifier.isdigit():
        proj = _encode_project(project_name)
        result = _request(f"{proj}/requirements/{identifier}", token)
        if not result.get("error"):
            return int(identifier)

    # Search by tag in the requirements list
    proj = _encode_project(project_name)
    result = _request(f"{proj}/requirements", token)
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
    """Resolve a test case tag (e.g. 'TC-42') or numeric ID to the internal API id."""
    if identifier.isdigit():
        proj = _encode_project(project_name)
        result = _request(f"{proj}/testCases/{identifier}", token)
        if not result.get("error"):
            return int(identifier)

    proj = _encode_project(project_name)
    result = _request(f"{proj}/testCases", token)
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

    # Normalize the URL — append the REST API path if not present
    normalized_url = url.rstrip("/")
    if not normalized_url.endswith("/helix-alm/api/v0"):
        normalized_url += "/helix-alm/api/v0/"
    elif not normalized_url.endswith("/"):
        normalized_url += "/"

    _session["helix_alm_url"] = normalized_url
    _session["helix_alm_user"] = username
    _session["helix_alm_password"] = password
    _session["helix_alm_api_key"] = api_key
    _session["helix_alm_api_secret"] = api_secret
    _session["helix_alm_ssl_verify"] = ssl_verify

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
    return json.dumps({
        "status": "connected",
        "url": normalized_url,
        "auth_method": "api_token" if has_apikey else "basic",
        "available_projects": [p["name"] for p in projects],
    }, indent=2)


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
        },
        "azure_devops": {
            "configured": _azdo_configured(),
            "organization": _session["azdo_org"] or "(not set)",
            "project": _session["azdo_project"] or "(not set)",
        },
    }
    return json.dumps(status, indent=2)


# --- MCP Tools ---

@mcp.tool()
def list_projects() -> str:
    """List all available projects in Helix ALM."""
    if not _helix_configured():
        return _helix_not_configured_msg()
    result = _request("projects")
    if result.get("error"):
        return f"Error listing projects: {json.dumps(result, indent=2)}"
    projects = result["data"].get("projects", [])
    names = [p["name"] for p in projects]
    return json.dumps({"projects": names}, indent=2)


@mcp.tool()
def list_requirements(project_name: str, columns: str = "", filter_name: str = "") -> str:
    """List requirements in a Helix ALM project.

    Args:
        project_name: Name of the Helix ALM project.
        columns: Comma-separated field labels to include (e.g. "Summary,Priority"). Leave empty for defaults.
        filter_name: Optional saved filter name to apply.
    """
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
        return f"Error listing requirements: {json.dumps(result, indent=2)}"

    reqs = result["data"].get("requirements", [])
    formatted = [_format_requirement(r) for r in reqs]
    return json.dumps({"count": len(formatted), "requirements": formatted}, indent=2)


@mcp.tool()
def get_requirement(project_name: str, requirement_identifier: str) -> str:
    """Get a single requirement by tag (e.g. 'BR-1960') or internal ID.

    Args:
        project_name: Name of the Helix ALM project.
        requirement_identifier: The requirement tag (e.g. 'BR-1960', 'FR-1961') or numeric ID.
    """
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    req_id = _resolve_requirement_id(project_name, token, requirement_identifier)
    if req_id is None:
        return f"Error: Could not find requirement '{requirement_identifier}'."

    proj = _encode_project(project_name)
    result = _request(f"{proj}/requirements/{req_id}", token)
    if result.get("error"):
        return f"Error getting requirement {requirement_identifier}: {json.dumps(result, indent=2)}"

    req = result["data"]
    summary = _format_requirement(req)
    all_fields = {}
    for f in req.get("fields", []):
        all_fields[f["label"]] = _get_field(req["fields"], f["label"])
    summary["all_fields"] = all_fields
    return json.dumps(summary, indent=2)


@mcp.tool()
def get_requirement_types(project_name: str) -> str:
    """Get the available requirement types for a project (e.g. Business Requirement, Functional Requirement).

    Args:
        project_name: Name of the Helix ALM project.
    """
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    proj = _encode_project(project_name)
    # Get a sample requirement to discover available types from the tag prefixes
    result = _request(f"{proj}/requirements", token)
    if result.get("error"):
        return f"Error: {json.dumps(result, indent=2)}"

    types_seen = {}
    for req in result["data"].get("requirements", []):
        rt = req.get("requirementType", {})
        if rt and rt.get("label"):
            types_seen[rt["label"]] = rt.get("id")

    return json.dumps({"requirement_types": [
        {"label": label, "id": rid} for label, rid in types_seen.items()
    ]}, indent=2)


@mcp.tool()
def create_requirement(project_name: str, summary: str, description: str = "",
                       requirement_type: str = "Functional Requirement",
                       priority: str = "",
                       additional_fields: str = "") -> str:
    """Create a new requirement in a Helix ALM project.

    Args:
        project_name: Name of the Helix ALM project.
        summary: The requirement summary/title.
        description: Detailed description of the requirement.
        requirement_type: Requirement type label (e.g. "Business Requirement", "Functional Requirement", "Non-Functional Requirement"). Use get_requirement_types to see available types.
        priority: Priority level (e.g. "High", "Medium", "Low"). Must match project config.
        additional_fields: JSON string of extra fields to set, e.g. '{"Category": "Security"}'.
    """
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
        return f"Error creating requirement: {json.dumps(result, indent=2)}"

    created_list = data.get("requirements", []) if isinstance(data, dict) else []
    created = created_list[0] if created_list else data
    return json.dumps({
        "message": "Requirement created successfully",
        "requirement": _format_requirement(created) if created else {},
    }, indent=2)


@mcp.tool()
def update_requirement(project_name: str, requirement_identifier: str,
                       summary: str = "", description: str = "",
                       priority: str = "", additional_fields: str = "") -> str:
    """Update an existing requirement in Helix ALM.

    Args:
        project_name: Name of the Helix ALM project.
        requirement_identifier: The requirement tag (e.g. 'BR-1960') or numeric ID.
        summary: New summary (leave empty to keep current).
        description: New description (leave empty to keep current).
        priority: New priority (leave empty to keep current).
        additional_fields: JSON string of extra fields to update, e.g. '{"Category": "Security"}'.
    """
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    req_id = _resolve_requirement_id(project_name, token, requirement_identifier)
    if req_id is None:
        return f"Error: Could not find requirement '{requirement_identifier}'."

    proj = _encode_project(project_name)

    get_result = _request(f"{proj}/requirements/{req_id}", token)
    if get_result.get("error"):
        return f"Error getting requirement {requirement_identifier}: {json.dumps(get_result, indent=2)}"

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
        return f"Error updating requirement: {json.dumps(result, indent=2)}"

    updated = result["data"] if result["data"] else req
    return json.dumps({
        "message": f"Requirement {requirement_identifier} updated successfully",
        "requirement": _format_requirement(updated),
    }, indent=2)


@mcp.tool()
def delete_requirement(project_name: str, requirement_identifier: str) -> str:
    """Delete a requirement from a Helix ALM project.

    Args:
        project_name: Name of the Helix ALM project.
        requirement_identifier: The requirement tag (e.g. 'BR-1960') or numeric ID.
    """
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    req_id = _resolve_requirement_id(project_name, token, requirement_identifier)
    if req_id is None:
        return f"Error: Could not find requirement '{requirement_identifier}'."

    proj = _encode_project(project_name)
    result = _request(f"{proj}/requirements/{req_id}", token, method="DELETE")
    if result.get("error"):
        return f"Error deleting requirement {requirement_identifier}: {json.dumps(result, indent=2)}"

    return json.dumps({"message": f"Requirement {requirement_identifier} deleted successfully"})


@mcp.tool()
def add_requirement_event(project_name: str, requirement_identifier: str,
                          event_name: str, notes: str = "") -> str:
    """Add a workflow event to a requirement (e.g. Comment, Approve, Reject).

    Args:
        project_name: Name of the Helix ALM project.
        requirement_identifier: The requirement tag (e.g. 'BR-1960') or numeric ID.
        event_name: The workflow event name (e.g. "Comment", "Approve", "Reject").
        notes: Optional notes/comments for the event.
    """
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
        return f"Error adding event: {json.dumps(result, indent=2)}"

    return json.dumps({
        "message": f"Event '{event_name}' added to requirement {requirement_identifier}",
        "data": result.get("data"),
    }, indent=2)


@mcp.tool()
def search_requirements(project_name: str, search_text: str) -> str:
    """Search requirements by text across Summary and Description fields.

    Args:
        project_name: Name of the Helix ALM project.
        search_text: Text to search for in requirements.
    """
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    proj = _encode_project(project_name)
    # Use the filter/search query parameter
    qs = f"?searchText={urllib.parse.quote(search_text)}"
    result = _request(f"{proj}/requirements{qs}", token)
    if result.get("error"):
        return f"Error searching requirements: {json.dumps(result, indent=2)}"

    reqs = result["data"].get("requirements", [])
    formatted = [_format_requirement(r) for r in reqs]
    return json.dumps({"count": len(formatted), "requirements": formatted}, indent=2)


@mcp.tool()
def get_requirement_workflow_events(project_name: str, requirement_identifier: str) -> str:
    """Get the available workflow events for a requirement.

    Args:
        project_name: Name of the Helix ALM project.
        requirement_identifier: The requirement tag (e.g. 'BR-1960') or numeric ID.
    """
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    req_id = _resolve_requirement_id(project_name, token, requirement_identifier)
    if req_id is None:
        return f"Error: Could not find requirement '{requirement_identifier}'."

    proj = _encode_project(project_name)
    result = _request(f"{proj}/requirements/{req_id}/availableEvents", token)
    if result.get("error"):
        return f"Error: {json.dumps(result, indent=2)}"

    return json.dumps(result["data"], indent=2)


# --- Test Case MCP Tools ---

@mcp.tool()
def create_test_case(
    project_name: str,
    summary: str,
    test_case_type: str = "Validation",
    description: str = "",
    priority: str = "",
    steps_json: str = "",
    additional_fields: str = "",
) -> str:
    """Create a new test case in a Helix ALM project.

    Args:
        project_name: Name of the Helix ALM project.
        summary: The test case summary/title.
        test_case_type: The Type field value (e.g. "Validation", "Functional", "Regression").
                        Must match a menu item configured in the project. Defaults to "Validation".
        description: Detailed description of the test case.
        priority: Priority level (e.g. "High", "Medium", "Low"). Must match project config.
        steps_json: Optional JSON array of test steps. Each step needs 'text' and optionally
                    'expectedResult'. Example: '[{"text": "Open login page", "expectedResult": "Login page displayed"}]'
        additional_fields: JSON string of extra fields to set, e.g. '{"Automated Test": true}'.
    """
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

    if steps_json:
        try:
            steps_input = json.loads(steps_json)
            if not isinstance(steps_input, list):
                return "Error: steps_json must be a JSON array."
            basic_steps = []
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
                basic_steps.append({
                    "type": "step",
                    "step": {
                        "number": i,
                        "text": s.get("text", ""),
                        "stepRows": step_rows,
                    },
                })
            body["steps"] = {
                "stepsData": {
                    "type": "basic",
                    "modifiedToCorrectSyntax": False,
                    "basic": basic_steps,
                    "detailed": [],
                }
            }
        except json.JSONDecodeError:
            return "Error: steps_json must be a valid JSON string."

    wrapped_body = {"testCases": [body]}
    result = _request(f"{proj}/testCases", token, wrapped_body, "POST")

    data = result.get("data", {})
    if result.get("error") or (isinstance(data, dict) and data.get("errors")):
        errors = data.get("errors", []) if isinstance(data, dict) else []
        created = data.get("testCases", []) if isinstance(data, dict) else []
        if created:
            tc = created[0]
            return json.dumps({
                "message": "Test case created with warnings",
                "test_case": {"id": tc.get("id"), "tag": tc.get("tag", ""), "summary": summary},
                "warnings": errors,
            }, indent=2)
        return f"Error creating test case: {json.dumps(result, indent=2)}"

    created_list = data.get("testCases", []) if isinstance(data, dict) else []
    created = created_list[0] if created_list else data
    return json.dumps({
        "message": "Test case created successfully",
        "test_case": {
            "id": created.get("id") if created else None,
            "tag": created.get("tag", "") if created else "",
            "summary": summary,
        },
    }, indent=2)


@mcp.tool()
def link_test_case_to_requirement(
    project_name: str,
    test_case_identifier: str,
    requirement_identifier: str,
    link_type: str = "Requirement Tested By",
) -> str:
    """Link a test case to an existing requirement in Helix ALM.

    Uses a parentChildren link where the requirement is the parent and the test case
    is the child, matching the standard 'Requirement Tested By' traceability link.

    Args:
        project_name: Name of the Helix ALM project.
        test_case_identifier: The test case tag (e.g. 'TC-42') or numeric ID.
        requirement_identifier: The requirement tag (e.g. 'BR-1960') or numeric ID.
        link_type: The link definition name to use. Defaults to 'Requirement Tested By',
                   the standard traceability link. Use 'Related Items' for a peer link.
    """
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
        return f"Error linking test case to requirement: {json.dumps(result, indent=2)}"

    return json.dumps({
        "message": f"Test case {test_case_identifier} linked to requirement {requirement_identifier}",
        "test_case_id": tc_id,
        "requirement_id": req_id,
        "link_type": link_type,
    }, indent=2)


# --- Document Tree helpers ---

def _resolve_document_id(project_name: str, token: str, identifier: str) -> int | None:
    """Resolve a document tag (e.g. 'RD-91') or numeric ID to the internal API id."""
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
def list_documents(project_name: str) -> str:
    """List all requirement documents in a Helix ALM project.

    Args:
        project_name: Name of the Helix ALM project.
    """
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    proj = _encode_project(project_name)
    result = _request(f"{proj}/documents", token)
    if result.get("error"):
        return f"Error listing documents: {json.dumps(result, indent=2)}"

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
def create_document(project_name: str, name: str) -> str:
    """Create a new requirement document in a Helix ALM project.

    Args:
        project_name: Name of the Helix ALM project.
        name: Name for the new requirement document.
    """
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
        return f"Error creating document: {json.dumps(result, indent=2)}"

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
def get_document_tree(project_name: str, document_identifier: str,
                      max_depth: int = 3) -> str:
    """Get the hierarchical tree structure of a requirement document, showing all
    sections and requirements organized in their outline order.

    Args:
        project_name: Name of the Helix ALM project.
        document_identifier: Document tag (e.g. 'RD-91') or numeric ID.
        max_depth: Maximum depth of tree to retrieve (default 3). Use higher values for deeply nested documents.
    """
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
def get_document_node_children(project_name: str, document_identifier: str,
                               node_id: int) -> str:
    """Get the direct child nodes of a specific node in a document tree.

    Args:
        project_name: Name of the Helix ALM project.
        document_identifier: Document tag (e.g. 'RD-91') or numeric ID.
        node_id: The node ID to get children for (from get_document_tree results).
    """
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    doc_id = _resolve_document_id(project_name, token, document_identifier)
    if doc_id is None:
        return f"Error: Could not find document '{document_identifier}'."

    proj = _encode_project(project_name)
    result = _request(f"{proj}/documentTrees/{doc_id}/nodes/{node_id}/childNodes", token)
    if result.get("error"):
        return f"Error: {json.dumps(result, indent=2)}"

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
def add_to_document_tree(project_name: str, document_identifier: str,
                         parent_node_id: int,
                         requirement_identifiers: str) -> str:
    """Add one or more requirements as child nodes under a parent node in a document tree.

    Args:
        project_name: Name of the Helix ALM project.
        document_identifier: Document tag (e.g. 'RD-91') or numeric ID.
        parent_node_id: The node ID to add children under (from get_document_tree results).
        requirement_identifiers: Comma-separated requirement tags or IDs to add (e.g. 'FR-2219,NFR-2220,CR-2222').
    """
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
        return f"Error adding to document tree: {json.dumps(result, indent=2)}"

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
def add_to_document_tree_top_level(project_name: str, document_identifier: str,
                                   requirement_identifiers: str) -> str:
    """Add one or more requirements as top-level nodes in a document tree.

    Args:
        project_name: Name of the Helix ALM project.
        document_identifier: Document tag (e.g. 'RD-91') or numeric ID.
        requirement_identifiers: Comma-separated requirement tags or IDs to add (e.g. 'OV-2246,OV-2247').
    """
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
        return f"Error adding to document tree: {json.dumps(result, indent=2)}"

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
def get_document_requirements(project_name: str, document_name: str) -> str:
    """Get all requirements that belong to a specific requirement document, grouped by type.
    This finds requirements via their 'Document List' field rather than the tree structure.

    Args:
        project_name: Name of the Helix ALM project.
        document_name: The document name (or partial name) to match in the Document List field.
    """
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    proj = _encode_project(project_name)
    result = _request(f"{proj}/requirements", token)
    if result.get("error"):
        return f"Error: {json.dumps(result, indent=2)}"

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


# --- Automation Suite MCP Tools ---

@mcp.tool()
def list_automation_suites(project_name: str) -> str:
    """List all automation suites in a Helix ALM project.

    Args:
        project_name: Name of the Helix ALM project.
    """
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    proj = _encode_project(project_name)
    result = _request(f"{proj}/automationSuites", token)
    if result.get("error"):
        return f"Error listing automation suites: {json.dumps(result, indent=2)}"

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
def create_automation_suite(project_name: str, name: str,
                            description: str = "") -> str:
    """Create a new automation suite in a Helix ALM project.

    Args:
        project_name: Name of the Helix ALM project.
        name: Name of the automation suite.
        description: Optional description.
    """
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
        return f"Error creating automation suite: {json.dumps(result, indent=2)}"

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
def get_automation_suite(project_name: str, suite_id: int) -> str:
    """Get details of a specific automation suite.

    Args:
        project_name: Name of the Helix ALM project.
        suite_id: The numeric ID of the automation suite.
    """
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    proj = _encode_project(project_name)
    result = _request(f"{proj}/automationSuites/{suite_id}", token)
    if result.get("error"):
        return f"Error getting automation suite {suite_id}: {json.dumps(result, indent=2)}"

    return json.dumps(result["data"], indent=2)


@mcp.tool()
def submit_automation_build(
    project_name: str,
    suite_id: int,
    build_number: str,
    results_json: str,
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
        project_name: Name of the Helix ALM project.
        suite_id: The numeric ID of the automation suite.
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
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

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
        return f"Error submitting build: {json.dumps(result, indent=2)}"

    return json.dumps({
        "message": f"Build {build_number} submitted to automation suite {suite_id}",
        "data": result.get("data"),
    }, indent=2)


@mcp.tool()
def submit_automation_results_simple(
    project_name: str,
    suite_id: int,
    build_number: str,
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
        project_name: Name of the Helix ALM project.
        suite_id: The numeric ID of the automation suite.
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
        suite_id=suite_id,
        build_number=build_number,
        results_json=json.dumps(results),
        description=description,
        branch=branch,
        external_url=external_url,
    )


@mcp.tool()
def list_automation_builds(project_name: str, suite_id: int) -> str:
    """List builds that have been submitted to an automation suite.

    Args:
        project_name: Name of the Helix ALM project.
        suite_id: The numeric ID of the automation suite.
    """
    token = _get_token(project_name)
    if not token:
        return _helix_not_configured_msg() if not _helix_configured() else f"Error: Could not get access token for project '{project_name}'."

    proj = _encode_project(project_name)
    result = _request(f"{proj}/automationSuites/{suite_id}/builds", token)
    if result.get("error"):
        return f"Error listing builds: {json.dumps(result, indent=2)}"

    return json.dumps(result["data"], indent=2)


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
    project_name: str,
    suite_id: int,
    build_number: str,
    file_path: str,
    branch: str = "",
    external_url: str = "",
    test_run_set: str = "",
) -> str:
    """Submit test results from a JUnit XML file to a Helix ALM automation suite.

    Args:
        project_name: Name of the Helix ALM project.
        suite_id: The numeric ID of the automation suite.
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
        suite_id=suite_id,
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
    project_name: str,
    suite_id: int,
    build_number: str,
    file_path: str,
    branch: str = "",
    external_url: str = "",
    test_run_set: str = "",
) -> str:
    """Submit test results from an xUnit v2 XML file to a Helix ALM automation suite.

    Args:
        project_name: Name of the Helix ALM project.
        suite_id: The numeric ID of the automation suite.
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
        suite_id=suite_id,
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
    project_name: str,
    suite_id: int,
    build_number: str,
    file_path: str,
    branch: str = "",
    external_url: str = "",
    test_run_set: str = "",
) -> str:
    """Submit test results from a JUnit or xUnit XML file, auto-detecting the format.

    Args:
        project_name: Name of the Helix ALM project.
        suite_id: The numeric ID of the automation suite.
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
        suite_id=suite_id,
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
        return f"Error: {json.dumps(result, indent=2)}"

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
        return f"Error: {json.dumps(result, indent=2)}"

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
        return f"Error fetching test runs: {json.dumps(runs_result, indent=2)}"

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
    build_id: int,
    helix_project_name: str,
    helix_suite_id: int,
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
        helix_project_name: Name of the Helix ALM project.
        helix_suite_id: The Helix ALM automation suite ID to submit results to.
        azdo_org: Azure DevOps organization name. Uses session config if empty.
        azdo_project: Azure DevOps project name. Uses session config if empty.
        azdo_pat: Personal access token. Uses session config if empty.
        build_number_override: Override the build number (defaults to Azure DevOps buildNumber).
        branch_override: Override the branch name (defaults to Azure DevOps sourceBranch).
    """
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
        return f"Error fetching build info: {json.dumps(build_result, indent=2)}"

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
        suite_id=helix_suite_id,
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
    helix_project_name: str,
    helix_suite_id: int,
    pipeline_id: int = 0,
    azdo_org: str = "",
    azdo_project: str = "",
    azdo_pat: str = "",
) -> str:
    """Fetch test results from the latest completed Azure DevOps build and submit
    them to a Helix ALM automation suite. Optionally filter by pipeline.

    Args:
        helix_project_name: Name of the Helix ALM project.
        helix_suite_id: The Helix ALM automation suite ID to submit results to.
        pipeline_id: Optional pipeline/definition ID to filter by. 0 means latest from any pipeline.
        azdo_org: Azure DevOps organization name. Uses session config if empty.
        azdo_project: Azure DevOps project name. Uses session config if empty.
        azdo_pat: Personal access token. Uses session config if empty.
    """
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
        return f"Error fetching builds: {json.dumps(result, indent=2)}"

    builds = result["data"].get("value", [])
    if not builds:
        return json.dumps({"error": "No completed builds found."})

    latest = builds[0]
    build_id = latest["id"]

    return azdo_submit_to_helix_alm(
        build_id=build_id,
        helix_project_name=helix_project_name,
        helix_suite_id=helix_suite_id,
        azdo_org=azdo_org,
        azdo_project=azdo_project,
        azdo_pat=azdo_pat,
    )


if __name__ == "__main__":
    mcp.run()
