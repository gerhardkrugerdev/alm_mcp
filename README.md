# Perforce ALM MCP Server

> **Note:** Helix ALM has been rebranded to **Perforce ALM**. This server targets the Perforce ALM (formerly Helix ALM) REST API. Environment variables, tool names, and configuration keys still use the `HELIX_ALM_` prefix for backwards compatibility.

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that connects Claude (and other MCP-compatible AI assistants) to the [Perforce ALM](https://www.perforce.com/products/helix-alm) (formerly Helix ALM) REST API. It lets you manage requirements, test cases, documents, and automation results directly from a conversation — with optional Azure DevOps integration for pulling CI/CD test results into Perforce ALM.

---

## Features

- **Requirements** — list, search, create, update, delete, and trigger workflow events
- **Test Cases** — create, read, and update test cases with structured steps; link them to requirements
- **Requirement Documents** — browse document trees, add requirements to sections
- **Automation Suites** — submit test run results (manually, from JUnit/xUnit XML, or from Azure DevOps builds)
- **Azure DevOps** — pull test results from a build and push them straight into a Perforce ALM automation suite
- **Session-based auth** — credentials are held in memory only and never written to disk

---

## Requirements

- Python 3.11+
- [`mcp`](https://pypi.org/project/mcp/) package (`pip install mcp`)
- A running Perforce ALM server (formerly Helix ALM, v24 or later recommended) with the REST API enabled
- An API token (recommended) or username/password

---

## Installation

```bash
git clone https://github.com/gerhardkrugerdev/alm_mcp.git
cd alm_mcp
pip install mcp
```

Copy the example environment file and fill in your details (optional — you can also configure everything through the `configure_helix_alm` tool at runtime):

```bash
cp .env.example .env
```

---

## Running the server

```bash
python helix_alm_mcp.py
```

The server communicates over **stdio**, which is the standard transport for MCP clients such as Claude Desktop and Claude Code.

### Claude Desktop configuration

Add this block to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "helix-alm": {
      "command": "python",
      "args": ["C:/path/to/alm_mcp/helix_alm_mcp.py"],
      "env": {
        "HELIX_ALM_URL": "https://your-server:8443/",
        "HELIX_ALM_API_KEY": "your-api-key",
        "HELIX_ALM_API_SECRET": "your-api-secret"
      }
    }
  }
}
```

### Claude Code configuration

Add the same block to your Claude Code `settings.json` under `"mcpServers"`.

---

## Authentication

The server supports two authentication methods:

| Method | Environment variables | `configure_helix_alm` argument |
|--------|----------------------|-------------------------------|
| API Token *(recommended)* | `HELIX_ALM_API_KEY` + `HELIX_ALM_API_SECRET` | `api_token="key:secret"` |
| Basic auth | `HELIX_ALM_USER` + `HELIX_ALM_PASSWORD` | `username=` + `password=` |

Credentials set via environment variables are loaded at startup. You can override them at any time by calling `configure_helix_alm` in the conversation.

---

## Tool reference

### Configuration

#### `configure_helix_alm`
Configure the Perforce ALM (formerly Helix ALM) connection for the current session.

| Argument | Description |
|----------|-------------|
| `url` | Server base URL, e.g. `https://your-server:8443/` |
| `api_token` | API token in `key:secret` format *(recommended)* |
| `username` / `password` | Basic auth alternative |
| `ssl_verify` | Verify SSL certificates (default `false` for self-signed certs) |

#### `configure_azure_devops`
Configure the Azure DevOps connection for the current session.

| Argument | Description |
|----------|-------------|
| `organization` | Azure DevOps organisation name |
| `project` | Azure DevOps project name |
| `pat` | Personal access token |

#### `get_connection_status`
Returns the current connection state for both Perforce ALM and Azure DevOps (without revealing credentials).

---

### Requirements

| Tool | Description |
|------|-------------|
| `list_projects` | List all projects available in Perforce ALM |
| `list_requirements` | List requirements in a project (supports column selection and saved filters) |
| `get_requirement` | Get full details of a single requirement by tag (e.g. `BR-1960`) or numeric ID |
| `get_requirement_types` | List the requirement types configured in a project |
| `create_requirement` | Create a new requirement with summary, description, type, priority, and custom fields |
| `update_requirement` | Update fields on an existing requirement |
| `delete_requirement` | Delete a requirement |
| `add_requirement_event` | Trigger a workflow event on a requirement (e.g. `Comment`, `Approve`) |
| `search_requirements` | Full-text search across Summary and Description fields |
| `get_requirement_workflow_events` | List the workflow events currently available for a requirement |

**Example — create a requirement:**
```
Create a functional requirement in "My Project" with summary "User login via SSO"
and description "The system must support SAML 2.0 SSO for all users."
```

---

### Test Cases

| Tool | Description |
|------|-------------|
| `create_test_case` | Create a new test case with optional steps (steps are added via a separate PUT to the `/steps` sub-resource after creation) |
| `get_test_case` | Get full details of a test case by tag or ID, including steps and linked items |
| `update_test_case` | Update fields and/or steps on an existing test case |
| `link_test_case_to_requirement` | Link a test case to a requirement using the "Requirement Tested By" traceability link |

#### `create_test_case` arguments

| Argument | Description |
|----------|-------------|
| `project_name` | Perforce ALM project name |
| `summary` | Test case title |
| `test_case_type` | Type menu item (e.g. `"Validation"`, `"Functional"`). Defaults to `"Validation"` |
| `description` | Detailed description |
| `priority` | Priority menu item value (if configured on test cases in your project) |
| `steps_json` | JSON array of steps — see format below |
| `additional_fields` | JSON object of extra field values |

**Steps format:**
```json
[
  {"text": "Open the login page", "expectedResult": "Login page is displayed"},
  {"text": "Enter valid credentials", "expectedResult": "User is redirected to the dashboard"}
]
```

> **Note:** The Perforce ALM REST API does not support adding steps inline during test case creation (POST). This server automatically handles this by creating the test case first, then adding steps via a PUT to the `/testCases/{id}/steps` sub-resource using the `detailed` step format.

#### `get_test_case` arguments

| Argument | Description |
|----------|-------------|
| `project_name` | Perforce ALM project name |
| `test_case_identifier` | Test case tag (e.g. `TC-382`) or numeric ID |

Returns the test case fields, all steps (with expected results), and linked items. Steps are fetched from the `/steps` sub-resource since the main test case endpoint does not include them.

#### `update_test_case` arguments

| Argument | Description |
|----------|-------------|
| `project_name` | Perforce ALM project name |
| `test_case_identifier` | Test case tag (e.g. `TC-382`) or numeric ID |
| `summary` | New summary (leave empty to keep current) |
| `description` | New description (leave empty to keep current) |
| `test_case_type` | New Type value (leave empty to keep current) |
| `steps_json` | JSON array of steps to replace existing steps (same format as `create_test_case`) |
| `additional_fields` | JSON object of extra field values |

All arguments are optional — only the fields you provide will be updated. Steps are replaced entirely (not merged).

#### `link_test_case_to_requirement` arguments

| Argument | Description |
|----------|-------------|
| `project_name` | Perforce ALM project name |
| `test_case_identifier` | Test case tag (e.g. `TC-42`) or numeric ID |
| `requirement_identifier` | Requirement tag (e.g. `BR-1960`) or numeric ID |
| `link_type` | Link definition name. Defaults to `"Requirement Tested By"` |

**Example:**
```
Create a test case in "My Project" titled "Verify SSO login flow" with type Validation
and steps: navigate to login, click SSO, verify redirect. Then link it to requirement BR-1960.
```

---

### Requirement Documents

| Tool | Description |
|------|-------------|
| `list_documents` | List all requirement documents in a project |
| `create_document` | Create a new requirement document |
| `get_document_tree` | Get the full hierarchical tree of a document (sections and requirements) |
| `get_document_node_children` | Get the direct children of a specific node in a document tree |
| `add_to_document_tree` | Add requirements as children under a specific node |
| `add_to_document_tree_top_level` | Add requirements as top-level nodes in a document |
| `get_document_requirements` | List all requirements belonging to a document (via Document List field) |

---

### Automation Suites

| Tool | Description |
|------|-------------|
| `list_automation_suites` | List all automation suites in a project |
| `create_automation_suite` | Create a new automation suite |
| `get_automation_suite` | Get details of a specific suite |
| `list_automation_builds` | List builds submitted to a suite |
| `submit_automation_build` | Submit test results as a build (full JSON format) |
| `submit_automation_results_simple` | Submit results using comma-separated pass/fail/skip lists |

---

### JUnit / xUnit XML

These tools let you submit test results directly from CI-generated XML files.

| Tool | Description |
|------|-------------|
| `submit_junit_results` | Submit results from a JUnit XML file |
| `submit_xunit_results` | Submit results from an xUnit v2 XML file |
| `submit_test_results_xml` | Auto-detect format (JUnit or xUnit) and submit |
| `preview_test_results_xml` | Parse and preview an XML file without submitting |

Sample XML files are provided in the `samples/` directory.

---

### Azure DevOps Integration

| Tool | Description |
|------|-------------|
| `azdo_list_pipelines` | List build pipelines in an Azure DevOps project |
| `azdo_list_builds` | List recent builds, optionally filtered by pipeline |
| `azdo_get_test_results` | Get test results from a specific build |
| `azdo_submit_to_helix_alm` | Fetch results from a build and submit them to a Perforce ALM automation suite |
| `azdo_submit_latest_to_helix_alm` | Same as above but automatically picks the latest completed build |

**Example end-to-end:**
```
Fetch the latest completed build from Azure DevOps pipeline 42 and submit
the test results to Perforce ALM automation suite 7 in project "My Project".
```

---

## Environment variables

All variables are optional — the server falls back to the `configure_*` tools if they are not set.

| Variable | Description |
|----------|-------------|
| `HELIX_ALM_URL` | Perforce ALM server base URL |
| `HELIX_ALM_USER` | Username for basic auth |
| `HELIX_ALM_PASSWORD` | Password for basic auth |
| `HELIX_ALM_API_KEY` | API key (preferred) |
| `HELIX_ALM_API_SECRET` | API secret (preferred) |
| `HELIX_ALM_SSL_VERIFY` | Set to `true` to enforce SSL certificate validation |
| `AZDO_ORG` | Azure DevOps organisation |
| `AZDO_PROJECT` | Azure DevOps project |
| `AZDO_PAT` | Azure DevOps personal access token |

---

## Security notes

- Credentials are **never written to disk** — they exist only in the process memory for the lifetime of the session.
- The `.env` file is excluded from version control via `.gitignore`. Use `.env.example` as a template.
- SSL verification is disabled by default to support self-signed certificates common in on-premise Perforce ALM deployments. Enable it (`ssl_verify=true`) when connecting to servers with a trusted certificate.
