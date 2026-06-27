import os
from datetime import datetime, date
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from dotenv import load_dotenv
from notion_client import Client
from jira import JIRA
from groq import Groq
from apscheduler.schedulers.background import BackgroundScheduler
import embedder  # RAG layer

load_dotenv(override=True)

# Slack setup
app = App(token=os.environ["SLACK_BOT_TOKEN"])
ALERT_CHANNEL = "C0BDP3SUGTT"  # channel where proactive alerts are posted

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

# Conversation memory — keyed by Slack thread_ts
# Each thread gets its own list of {"role": ..., "content": ...} dicts
conversation_memory = {}
MAX_HISTORY = 10  # max turns to keep per thread (avoids token overflow)


# ── Data fetchers ────────────────────────────────────────────────────

def get_all_vendors():
    results = notion.databases.query(database_id=DATABASE_ID)
    vendors = []
    for page in results.get("results", []):
        props = page["properties"]
        vendors.append({
            "name":        props["Vendor Name"]["title"][0]["plain_text"] if props["Vendor Name"]["title"] else "",
            "status":      props["Contract Status"]["select"]["name"] if props["Contract Status"]["select"] else "",
            "deliverable": props["Deliverable"]["rich_text"][0]["plain_text"] if props["Deliverable"]["rich_text"] else "",
            "due_date":    props["Due Date"]["rich_text"][0]["plain_text"] if props["Due Date"]["rich_text"] else "",
            "notes":       props["Notes"]["rich_text"][0]["plain_text"] if props["Notes"]["rich_text"] else "",
        })
    return vendors


def get_all_tickets():
    issues = jira.search_issues('updated >= "-365d" ORDER BY updated DESC', maxResults=50)
    return [
        {
            "key":      issue.key,
            "summary":  issue.fields.summary,
            "status":   issue.fields.status.name,
            "priority": issue.fields.priority.name if issue.fields.priority else "None",
        }
        for issue in issues
    ]


def refresh_index():
    """Re-fetch from Notion + Jira and rebuild the vector index."""
    print("[casey] Refreshing vector index...")
    embedder.index_data(get_all_vendors(), get_all_tickets())


# ── Block Kit helpers ────────────────────────────────────────────────

def build_alert_blocks(today_str, expired, overdue, blocked):
    """Build a rich Slack Block Kit message for proactive alerts."""
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🤖 Casey's Daily Alert — {today_str}", "emoji": True},
        },
        {"type": "divider"},
    ]

    if expired:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"🚨 *Expired Contracts — {len(expired)} vendor(s)*"},
        })
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(f"• *{v['name']}* — {v['deliverable']}" for v in expired)},
        })
        blocks.append({"type": "divider"})

    if overdue:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"⚠️ *Overdue Deliverables — {len(overdue)} item(s)*"},
        })
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(overdue)},
        })
        blocks.append({"type": "divider"})

    if blocked:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"🔴 *Blocked Tickets — {len(blocked)} ticket(s)*"},
        })
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "\n".join(
                    f"• *{t['key']}* — {t['summary']} ({t['status']}, {t['priority']} priority)"
                    for t in blocked
                ),
            },
        })

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"_Casey checks automatically every hour · Next check in ~60 min_"}],
    })

    return blocks


# ── Proactive alerts ─────────────────────────────────────────────────

def run_alerts():
    """
    Checks for: expired contracts, overdue deliverables, blocked tickets.
    Posts a summary to the Slack alert channel if issues are found.
    Runs on a schedule via APScheduler.
    """
    print("[alerts] Running alert check...")
    today = date.today()
    current_year = today.year
    alerts = []

    vendors = get_all_vendors()
    tickets = get_all_tickets()

    # 1. Expired contracts
    expired = [v for v in vendors if v["status"] == "Expired"]
    if expired:
        lines = "\n".join(f"- {v['name']} ({v['deliverable']})" for v in expired)
        alerts.append(f"🚨 *Expired Contracts ({len(expired)}):*\n{lines}")

    # 2. Overdue deliverables (due date has passed, contract not already expired)
    overdue = []
    for v in vendors:
        if v["due_date"] and v["status"] != "Expired":
            try:
                due = datetime.strptime(f"{v['due_date']} {current_year}", "%B %d %Y").date()
                if due < today:
                    overdue.append(f"- {v['name']}: {v['deliverable']} (was due {v['due_date']})")
            except ValueError:
                pass  # skip unparseable dates
    if overdue:
        alerts.append(f"⚠️ *Overdue Deliverables ({len(overdue)}):*\n" + "\n".join(overdue))

    # 3. Blocked tickets
    blocked = [
        t for t in tickets
        if "blocked" in t["summary"].lower() or "blocked" in t["status"].lower()
    ]
    if blocked:
        lines = "\n".join(f"- {t['key']}: {t['summary']} ({t['status']}, {t['priority']} priority)" for t in blocked)
        alerts.append(f"🔴 *Blocked Tickets ({len(blocked)}):*\n{lines}")

    # Post to Slack using Block Kit if there's anything to report
    if any([expired, overdue, blocked]):
        blocks = build_alert_blocks(today.strftime("%B %d, %Y"), expired, overdue, blocked)
        app.client.chat_postMessage(
            channel=ALERT_CHANNEL,
            text=f"Casey's Daily Alert — {today.strftime('%B %d, %Y')}",  # fallback for notifications
            blocks=blocks,
        )
        print(f"[alerts] Posted Block Kit alert ({len(expired)} expired, {len(overdue)} overdue, {len(blocked)} blocked)")
    else:
        print("[alerts] All clear — nothing to report")


# ── RAG-powered answer ───────────────────────────────────────────────

def ask_casey(question: str, history: list = []) -> str:
    today = date.today().strftime("%B %d, %Y")

    # For follow-up questions, augment the search query with the previous
    # user message so ChromaDB has enough context to find the right docs.
    search_query = question
    if history:
        last_user_msg = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
        search_query = f"{last_user_msg} {question}"

    relevant_docs = embedder.search(search_query, top_k=10)

    # Hybrid retrieval: check search_query (which includes conversation context)
    # so follow-up questions like "what were they working on?" also trigger
    # the metadata filter when the thread is about expired/pending vendors.
    query_context = search_query.lower()
    if "expired" in query_context:
        relevant_docs = list(dict.fromkeys(embedder.get_by_status("Expired") + relevant_docs))
    elif "pending" in query_context:
        relevant_docs = list(dict.fromkeys(embedder.get_by_status("Pending") + relevant_docs))

    if not relevant_docs:
        return "I don't have enough indexed data to answer that. Try asking me again in a moment."

    context = "\n".join(f"- {doc}" for doc in relevant_docs)

    history_instruction = (
        "\nIMPORTANT: The conversation history above shows what was discussed before. "
        "When the user says 'they', 'those', 'these vendors', 'that ticket', etc., "
        "refer ONLY to the specific items mentioned in the previous messages — "
        "do not include other items from the retrieved documents."
        if history else ""
    )

    system_prompt = f"""You are Casey, a helpful Slack assistant for a Business Analyst.
Today's date is {today}.

The following documents were retrieved as most relevant to the user's question
(via semantic search over vendor and sprint ticket data):

{context}

Answer using ONLY the information above. Be concise and use Slack-friendly formatting
(*bold* for emphasis, bullet points with "-"). If the data isn't sufficient, say so honestly.
{history_instruction}"""

    # Build messages: system prompt + conversation history + new question
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": question})

    response = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=0.3,
    )
    return response.choices[0].message.content


# ── Slack handlers ───────────────────────────────────────────────────

@app.message("hello")
def say_hello(message, say):
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"👋 Hey <@{message['user']}>, I'm Casey!", "emoji": True},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "I'm your AI-powered BA assistant. I can answer questions about your "
                    "*vendors* and *sprint tickets* using semantic search.\n\n"
                    "*Try asking me:*\n"
                    "• _Which vendors have expired contracts?_\n"
                    "• _What's blocking the sprint?_\n"
                    "• _Give me a summary of this week's priorities_"
                ),
            },
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "💡 Type *refresh* to sync latest data · *alert now* to trigger an alert check"}
            ],
        },
    ]
    say(blocks=blocks, text=f"Hey <@{message['user']}>, I'm Casey!")


@app.message("refresh")
def handle_refresh(message, say):
    say("Refreshing my knowledge base... 🔄")
    refresh_index()
    say("Done! I'm up to date with the latest Notion and Jira data. ✅")


@app.message("alert now")
def handle_alert_now(message, say):
    """Trigger an immediate alert check — useful for testing."""
    say("Running alert check now... 🔍")
    run_alerts()
    say("Alert check complete! Check the alerts channel. ✅")


@app.message("")
def handle_message(message, say):
    text = message["text"]

    # thread_ts identifies the thread — use message ts if it's a new top-level message
    thread_ts = message.get("thread_ts", message["ts"])

    # Retrieve existing history for this thread (empty list if new thread)
    history = conversation_memory.get(thread_ts, [])

    say(text="Thinking... 🤔", thread_ts=thread_ts)
    answer = ask_casey(text, history)

    # Save this turn to memory (trim to last MAX_HISTORY turns)
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": answer})
    conversation_memory[thread_ts] = history[-MAX_HISTORY:]

    say(text=answer, thread_ts=thread_ts)


# ── Startup ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Build vector index
    print("Building vector index from Notion + Jira data...")
    refresh_index()

    # Start background scheduler — runs alerts every hour
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_alerts, "interval", hours=1, id="alerts")
    scheduler.start()
    print("[scheduler] Alert job scheduled — runs every hour")

    # Run an immediate alert check on startup
    run_alerts()

    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("Casey is running!")
    handler.start()