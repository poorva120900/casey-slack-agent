"""
mcp_server.py — Casey's MCP Server

Exposes Casey's data tools via the Model Context Protocol (MCP).
External tools (Claude Desktop, Cursor, other agents) can connect to
this server and call Casey's tools to access vendor and sprint data.

Run standalone:
    python mcp_server.py

Or connect via Claude Desktop by adding to claude_desktop_config.json:
    {
      "mcpServers": {
        "casey": {
          "command": "python",
          "args": ["path/to/mcp_server.py"]
        }
      }
    }
"""

import os
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from notion_client import Client
from jira import JIRA

load_dotenv()

# Initialize FastMCP server
mcp = FastMCP("Casey")

# Notion setup
notion = Client(auth=os.environ["NOTION_TOKEN"])
DATABASE_ID = os.environ["NOTION_DATABASE_ID"]

# Jira setup
jira = JIRA(
    server=os.environ["JIRA_URL"],
    basic_auth=(os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"]),
)


# ── Helper ───────────────────────────────────────────────────────────


def _fetch_vendors() -> list[dict]:
    results = notion.databases.query(database_id=DATABASE_ID)
    vendors = []
    for page in results.get("results", []):
        props = page["properties"]
        vendors.append(
            {
                "name": props["Vendor Name"]["title"][0]["plain_text"]
                if props["Vendor Name"]["title"]
                else "",
                "status": props["Contract Status"]["select"]["name"]
                if props["Contract Status"]["select"]
                else "",
                "deliverable": props["Deliverable"]["rich_text"][0]["plain_text"]
                if props["Deliverable"]["rich_text"]
                else "",
                "due_date": props["Due Date"]["rich_text"][0]["plain_text"]
                if props["Due Date"]["rich_text"]
                else "",
                "notes": props["Notes"]["rich_text"][0]["plain_text"]
                if props["Notes"]["rich_text"]
                else "",
            }
        )
    return vendors


def _fetch_tickets() -> list[dict]:
    issues = jira.search_issues(
        'updated >= "-365d" ORDER BY updated DESC', maxResults=50
    )
    return [
        {
            "key": issue.key,
            "summary": issue.fields.summary,
            "status": issue.fields.status.name,
            "priority": issue.fields.priority.name if issue.fields.priority else "None",
        }
        for issue in issues
    ]


# ── MCP Tools ────────────────────────────────────────────────────────


@mcp.tool()
def get_all_vendors() -> list[dict]:
    """
    Retrieve all vendors from the Notion database.
    Returns name, contract status, deliverable, due date, and notes for each vendor.
    """
    return _fetch_vendors()


@mcp.tool()
def get_vendors_by_status(status: str) -> list[dict]:
    """
    Retrieve vendors filtered by contract status.

    Args:
        status: Contract status to filter by. One of: 'Active', 'Expired', 'Pending'.

    Returns a list of vendors matching the given status.
    """
    return [v for v in _fetch_vendors() if v["status"].lower() == status.lower()]


@mcp.tool()
def get_all_tickets() -> list[dict]:
    """
    Retrieve all active sprint tickets from Jira.
    Returns key, summary, status, and priority for each ticket.
    """
    return _fetch_tickets()


@mcp.tool()
def get_blocked_tickets() -> list[dict]:
    """
    Retrieve all Jira tickets that are blocked.
    Checks both the ticket summary and status for 'blocked' keyword.
    """
    return [
        t
        for t in _fetch_tickets()
        if "blocked" in t["summary"].lower() or "blocked" in t["status"].lower()
    ]


@mcp.tool()
def get_overdue_vendors() -> list[dict]:
    """
    Retrieve all vendors whose deliverable due date has already passed
    and whose contract is not already marked as Expired.
    Returns vendor name, deliverable, and due date.
    """
    from datetime import datetime, date

    today = date.today()
    current_year = today.year
    overdue = []
    for v in _fetch_vendors():
        if v["due_date"] and v["status"] != "Expired":
            try:
                due = datetime.strptime(
                    f"{v['due_date']} {current_year}", "%B %d %Y"
                ).date()
                if due < today:
                    overdue.append(
                        {
                            "name": v["name"],
                            "deliverable": v["deliverable"],
                            "due_date": v["due_date"],
                            "status": v["status"],
                        }
                    )
            except ValueError:
                pass
    return overdue


# ── Entry point ──────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Casey MCP Server starting...")
    mcp.run()
