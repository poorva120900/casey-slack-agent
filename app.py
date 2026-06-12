import os
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from dotenv import load_dotenv
from notion_client import Client
from jira import JIRA
from groq import Groq

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

# Groq setup
groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
GROQ_MODEL = "llama-3.3-70b-versatile"


# ── Notion: fetch all vendors ───────────────────────────────────────
def get_all_vendors():
    results = notion.databases.query(database_id=DATABASE_ID)
    pages = results.get("results", [])

    vendors = []
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
        vendors.append(
            {
                "name": name,
                "status": status,
                "deliverable": deliverable,
                "due_date": due_date,
                "notes": notes,
            }
        )
    return vendors


# ── Jira: fetch all sprint tickets ──────────────────────────────────
def get_all_tickets():
    issues = jira.search_issues(
        'updated >= "-365d" ORDER BY updated DESC', maxResults=50
    )
    tickets = []
    for issue in issues:
        tickets.append(
            {
                "key": issue.key,
                "summary": issue.fields.summary,
                "status": issue.fields.status.name,
                "priority": issue.fields.priority.name
                if issue.fields.priority
                else "None",
            }
        )
    return tickets


# ── Groq: understand the question and answer using real data ───────
def ask_casey(question):
    vendors = get_all_vendors()
    tickets = get_all_tickets()

    system_prompt = f"""You are Casey, a helpful Slack agent for a Business Analyst.
You have access to two data sources:

VENDOR DATA (from Notion):
{vendors}

SPRINT TICKETS (from Jira):
{tickets}

Answer the user's question using ONLY the data above. Be concise and use Slack-friendly formatting
(use *bold* for emphasis, bullet points with "-"). If the question relates to both vendors and
sprint tickets, combine relevant info from both. If you don't have enough data to answer, say so honestly.
"""

    response = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
        temperature=0.3,
    )

    return response.choices[0].message.content


# ── Slack handlers ──────────────────────────────────────────────────
@app.message("hello")
def say_hello(message, say):
    say(
        f"Hey there <@{message['user']}>! I'm Casey, your AI-powered assistant.\n\n"
        f"Ask me anything about vendors or sprint tickets, e.g.:\n"
        f"• 'Which vendors have contracts expiring soon?'\n"
        f"• 'What's blocking the release?'\n"
        f"• 'Give me a summary of this week's priorities'"
    )


@app.message("")
def handle_message(message, say):
    text = message["text"]
    say("Thinking... 🤔")
    answer = ask_casey(text)
    say(answer)


if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("Casey is running!")
    handler.start()
