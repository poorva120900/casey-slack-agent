import os
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from dotenv import load_dotenv
from notion_client import Client
from jira import JIRA
from groq import Groq
import embedder  # RAG layer

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


# ── Data fetchers ────────────────────────────────────────────────────


def get_all_vendors():
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


def get_all_tickets():
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


def refresh_index():
    """Re-fetch from Notion + Jira and rebuild the vector index."""
    print("[casey] Refreshing vector index...")
    embedder.index_data(get_all_vendors(), get_all_tickets())


# ── RAG-powered answer ───────────────────────────────────────────────


def ask_casey(question: str) -> str:
    from datetime import date

    today = date.today().strftime("%B %d, %Y")

    # Semantic search: retrieve only the most relevant docs
    relevant_docs = embedder.search(question, top_k=6)

    if not relevant_docs:
        return "I don't have enough indexed data to answer that. Try asking me again in a moment."

    context = "\n".join(f"- {doc}" for doc in relevant_docs)

    system_prompt = f"""You are Casey, a helpful Slack assistant for a Business Analyst.
Today's date is {today}.

The following documents were retrieved as most relevant to the user's question
(via semantic search over vendor and sprint ticket data):

{context}

Answer using ONLY the information above. Be concise and use Slack-friendly formatting
(*bold* for emphasis, bullet points with "-"). If the data isn't sufficient, say so honestly.
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


# ── Slack handlers ───────────────────────────────────────────────────


@app.message("hello")
def say_hello(message, say):
    say(
        f"Hey there <@{message['user']}>! I'm Casey, your AI-powered assistant.\n\n"
        f"Ask me anything about vendors or sprint tickets, e.g.:\n"
        f"• 'Which vendors have contracts expiring soon?'\n"
        f"• 'What's blocking the release?'\n"
        f"• 'Give me a summary of this week's priorities'"
    )


@app.message("refresh")
def handle_refresh(message, say):
    say("Refreshing my knowledge base... 🔄")
    refresh_index()
    say("Done! I'm up to date with the latest Notion and Jira data. ✅")


@app.message("")
def handle_message(message, say):
    text = message["text"]
    say("Thinking... 🤔")
    answer = ask_casey(text)
    say(answer)


# ── Startup ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Building vector index from Notion + Jira data...")
    refresh_index()

    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("Casey is running!")
    handler.start()
