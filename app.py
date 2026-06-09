import os
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from dotenv import load_dotenv
from notion_client import Client
from jira import JIRA

load_dotenv(override=True)

# Slack setup
app = App(token=os.environ["SLACK_BOT_TOKEN"])

# Notion setup
notion = Client(auth=os.environ["NOTION_TOKEN"])
DATABASE_ID = os.environ["NOTION_DATABASE_ID"]

# Jira setup
jira = JIRA(
    server=os.environ["JIRA_URL"],
    basic_auth=(os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"]),
)


# ── Notion: search vendors ──────────────────────────────────────────
def search_vendors(query):
    results = notion.databases.query(database_id=DATABASE_ID)
    pages = results.get("results", [])

    matched = []
    for page in pages:
        props = page["properties"]
        name = (
            props["Vendor Name"]["title"][0]["plain_text"]
            if props["Vendor Name"]["title"]
            else ""
        )
        status = (
            props["Contract Status"]["select"]["name"]
            if props["Contract Status"]["select"]
            else ""
        )
        deliverable = (
            props["Deliverable"]["rich_text"][0]["plain_text"]
            if props["Deliverable"]["rich_text"]
            else ""
        )
        due_date = (
            props["Due Date"]["rich_text"][0]["plain_text"]
            if props["Due Date"]["rich_text"]
            else ""
        )
        notes = (
            props["Notes"]["rich_text"][0]["plain_text"]
            if props["Notes"]["rich_text"]
            else ""
        )

        all_fields = f"{name} {status} {deliverable} {due_date} {notes}".lower()
        if query.lower() in all_fields:
            matched.append(
                {
                    "name": name,
                    "status": status,
                    "deliverable": deliverable,
                    "due_date": due_date,
                    "notes": notes,
                }
            )

    if not matched:
        return f"No vendor results found for '{query}'"

    response = f"Found *{len(matched)}* vendor result(s) for '*{query}*':\n\n"
    for v in matched:
        response += f"*{v['name']}*\n Status: {v['status']}\n Deliverable: {v['deliverable']}\n Due Date: {v['due_date']}\n Notes: {v['notes']}\n\n"
    return response


# ── Jira: search sprint tickets ─────────────────────────────────────
def search_jira(query):
    q = query.lower().strip()

    # Detect priority searches
    if q in ["high", "medium", "low"]:
        jql = f'priority = "{q.capitalize()}" ORDER BY updated DESC'
    # Detect status searches
    elif q in ["in progress", "to do", "done"]:
        jql = f'status = "{q}" ORDER BY updated DESC'
    # Detect blocked searches
    elif "block" in q:
        jql = 'summary ~ "BLOCKED" ORDER BY updated DESC'
    # General keyword search
    else:
        jql = f'summary ~ "{query}" OR description ~ "{query}" ORDER BY updated DESC'

    issues = jira.search_issues(jql, maxResults=5)

    if not issues:
        return f"No Jira tickets found for '{query}'"

    response = f"Found *{len(issues)}* ticket(s) for '*{query}*':\n\n"
    for issue in issues:
        response += f"*{issue.key}* — {issue.fields.summary}\n Status: {issue.fields.status.name}\n Priority: {issue.fields.priority.name}\n\n"
    return response


# ── Message router ──────────────────────────────────────────────────
def route_query(text):
    jira_keywords = [
        "ticket",
        "sprint",
        "blocked",
        "blocking",
        "release",
        "shipped",
        "deploy",
        "bug",
        "jira",
        "issue",
        "in progress",
        "to do",
        "high",
        "medium",
        "low",
        "priority",
        "scrum",
        "BLOCKED",
    ]
    if any(keyword in text.lower() for keyword in jira_keywords):
        return "jira"
    return "notion"


# ── Slack handlers ──────────────────────────────────────────────────
@app.message("hello")
def say_hello(message, say):
    say(
        f"Hey there <@{message['user']}>! I'm Casey.\n\nAsk me about:\n• *Vendors* — e.g. 'TechCorp' or 'Expired contracts'\n• *Sprint* — e.g. 'blocked tickets' or 'what is in progress'"
    )


@app.message("")
def handle_message(message, say):
    text = message["text"]
    destination = route_query(text)

    if destination == "jira":
        say(f"Searching Jira for: *{text}*...")
        result = search_jira(text)
    else:
        say(f"Searching vendors for: *{text}*...")
        result = search_vendors(text)

    say(result)


if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("Casey is running!")
    handler.start()
