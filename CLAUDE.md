# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

MCP (Model Context Protocol) server that exposes the Perforce ALM (formerly Helix ALM) REST API as tools for AI assistants. Also integrates with Azure DevOps to pull CI/CD test results into Perforce ALM. The entire server is a single Python file (`helix_alm_mcp.py`, ~2600 lines) using the `mcp` package's `FastMCP` framework.

> **Branding note:** Helix ALM has been rebranded to Perforce ALM. Code, env vars, and tool names still use the `HELIX_ALM_` / `helix_alm` prefix for backwards compatibility.

## Running

```bash
pip install mcp          # only dependency
python helix_alm_mcp.py  # runs over stdio (MCP transport)
```

No build step, no test suite, no linter configured.

## Architecture

**Single-file server** — all tools, helpers, and session state live in `helix_alm_mcp.py`.

### Session state

A module-level `_session` dict holds credentials in memory (never persisted). Populated from env vars at import time, overridable at runtime via `configure_helix_alm` / `configure_azure_devops` tools.

### HTTP layer

Uses `urllib.request` directly (no `requests` dependency). Two parallel HTTP stacks:
- **`_request()`** — Perforce ALM REST API. Supports `Bearer` (project token), `ApiKey`, and `Basic` auth. Handles SSL context for self-signed certs.
- **`_azdo_request()`** — Azure DevOps REST API. Basic auth with PAT.

Both return a uniform `{"status": int, "data": ..., "error": bool}` dict.

### Auth flow for Perforce ALM

Most tools call `_get_token(project_name)` first, which hits `{project}/token` to get a short-lived bearer token. That token is then passed to `_request()` for subsequent calls. If the token request fails, the tool reports an error.

### Key helper patterns

- **`_resolve_*_id()`** — Resolve user-facing tags (e.g. `BR-1960`, `TC-42`, `RD-91`) to internal numeric IDs by listing all items and matching. Three variants: `_resolve_requirement_id`, `_resolve_test_case_id`, `_resolve_document_id`.
- **`_get_field()` / `_set_field()`** — Read/write fields from the ALM fields array, dispatching on field `type` (`formattedString`, `menuItem`, `user`, etc.).
- **`_build_steps_payload()`** — Converts simple `[{text, expectedResult}]` JSON into the API's `detailed` steps format. Steps can't be created inline with a test case — they require a separate `PUT` to `/testCases/{id}/steps` via `_put_steps()`.
- **`_build_tree_recursive()`** — Recursively fetches document tree nodes for requirement documents.

### Tool groups (all registered via `@mcp.tool()`)

| Group | Prefix/Pattern | Lines |
|-------|---------------|-------|
| Configuration | `configure_helix_alm`, `configure_azure_devops`, `get_connection_status` | ~294–427 |
| Requirements | `list_projects`, `list_requirements`, `get_requirement`, `create_requirement`, `update_requirement`, `delete_requirement`, `add_requirement_event`, `search_requirements`, `get_requirement_workflow_events`, `get_requirement_types` | ~431–793 |
| Test Cases | `create_test_case`, `get_test_case`, `update_test_case`, `link_test_case_to_requirement` | ~795–1157 |
| Documents | `list_documents`, `create_document`, `get_document_tree`, `get_document_node_children`, `add_to_document_tree`, `add_to_document_tree_top_level`, `get_document_requirements` | ~1237–1580 |
| Automation | `list_automation_suites`, `create_automation_suite`, `get_automation_suite`, `submit_automation_build`, `submit_automation_results_simple`, `list_automation_builds` | ~1583–1828 |
| XML parsing | `submit_junit_results`, `submit_xunit_results`, `submit_test_results_xml`, `preview_test_results_xml` | ~2012–2223 |
| Azure DevOps | `azdo_list_pipelines`, `azdo_list_builds`, `azdo_get_test_results`, `azdo_submit_to_helix_alm`, `azdo_submit_latest_to_helix_alm` | ~2274–2583 |

### API quirks to know

- **Test case steps** cannot be added during creation (POST). The server works around this by creating the test case first, then PUTting steps to the `/steps` sub-resource.
- **Link bodies** use a `parentChildren` structure with `itemType` strings like `"requirements"` and `"testCases"`.
- The Perforce ALM REST API uses **project-scoped URLs**: `{project_name}/requirements`, `{project_name}/testCases`, etc. Project names are URL-encoded via `_encode_project()`.

## Environment variables

Configured in `.env` (git-ignored). See `.env.example` for the template. All optional — credentials can be set at runtime via the `configure_*` tools.

| Variable | Purpose |
|----------|---------|
| `HELIX_ALM_URL` | Server base URL |
| `HELIX_ALM_API_KEY` / `HELIX_ALM_API_SECRET` | API key auth (preferred) |
| `HELIX_ALM_USER` / `HELIX_ALM_PASSWORD` | Basic auth fallback |
| `HELIX_ALM_SSL_VERIFY` | `true` to enforce SSL validation (default: `false`) |
| `AZDO_ORG` / `AZDO_PROJECT` / `AZDO_PAT` | Azure DevOps connection |

## Samples

`samples/` contains JUnit and xUnit XML files for testing the XML parsing/submission tools.
