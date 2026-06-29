import os
import json
from datetime import datetime, date
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from dotenv import load_dotenv
from groq import Groq
from apscheduler.schedulers.background import BackgroundScheduler

# Reuse the same data functions as the MCP server — single source of truth
from mcp_server import _fetch_vendors, _fetch_tickets

load_dotenv(override=True)

# Slack setup
app = App(token=os.environ["SLACK_BOT_TOKEN"])
ALERT_CHANNEL = "C0BDP3SUGTT"

# Groq setup
groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
GROQ_MODEL = "openai/gpt-oss-120b"  # Groq's recommended model for tool use (replaces llama-3.3-70b-versatile)

# Conversation memory — keyed by Slack thread_ts
conversation_memory = {}
MAX_HISTORY = 10


# ── Groq Tool Definitions (mirror MCP server tools) ──────────────────
# These tell Groq what tools Casey has available so it can decide
# which one(s) to call based on the user's question.

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_all_vendors",
            "description": (
                "Retrieve all vendors from the Notion database. "
                "Returns name, contract status, deliverable, due date, and notes for each vendor. "
                "Use for general vendor questions or when you need the full picture."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_vendors_by_status",
            "description": (
                "Retrieve vendors filtered by contract status. "
                "Use this when the question specifically asks about Active, Expired, or Pending vendors."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Contract status to filter by.",
                        "enum": ["Active", "Expired", "Pending"],
                    }
                },
                "required": ["status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_all_tickets",
            "description": (
                "Retrieve all active sprint tickets from Jira. "
                "Returns key, summary, status, and priority for each ticket. "
                "Use for general sprint or ticket questions."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_blocked_tickets",
            "description": (
                "Retrieve all Jira tickets that are blocked. "
                "Use when the question is about blockers, blocked work, or what's holding up the sprint."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_overdue_vendors",
            "description": (
                "Retrieve all vendors whose deliverable due date has already passed "
                "and whose contract is not already marked as Expired. "
                "Use when the question is about overdue, late, or missed deliverables."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


# ── Tool Execution ────────────────────────────────────────────────────
# When Groq decides to call a tool, this function runs the actual logic
# using the same data functions as the MCP server.


def execute_tool(name: str, args: dict) -> list:
    if name == "get_all_vendors":
        return _fetch_vendors()

    elif name == "get_vendors_by_status":
        status = args.get("status", "Active")
        return [v for v in _fetch_vendors() if v["status"].lower() == status.lower()]

    elif name == "get_all_tickets":
        return _fetch_tickets()

    elif name == "get_blocked_tickets":
        return [
            t
            for t in _fetch_tickets()
            if "blocked" in t["summary"].lower() or "blocked" in t["status"].lower()
        ]

    elif name == "get_overdue_vendors":
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
                        overdue.append(v)
                except ValueError:
                    pass
        return overdue

    return []


# ── Agentic Q&A ───────────────────────────────────────────────────────
# Groq function calling loop:
# 1. Send question + tool definitions to Groq
# 2. Groq decides which tool(s) to call
# 3. Execute the tool(s) and return results to Groq
# 4. Groq uses the real data to generate a final answer
# 5. Repeat if Groq wants to call more tools (max 5 iterations)


def ask_casey(question: str, history: list = []) -> str:
    today = date.today().strftime("%B %d, %Y")

    system_prompt = f"""You are Casey, a helpful Slack assistant for a Business Analyst.
Today's date is {today}.

You have tools that fetch real-time data from Notion (vendor database) and Jira (sprint tickets).
- Use the most specific tool for the question (e.g. get_blocked_tickets instead of get_all_tickets when asked about blockers).
- Call multiple tools if the question spans both vendors and tickets.
- After getting results, give a concise answer using Slack-friendly formatting (*bold*, bullet points with "-").
- Use the conversation history to understand references like "they" or "those vendors".
"""

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": question})

    # Agentic loop
    for iteration in range(5):
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.3,
        )

        msg = response.choices[0].message

        # No tool calls — Groq has enough info to answer
        if not msg.tool_calls:
            return msg.content

        # Add Groq's tool call decision to message history (must be a plain dict)
        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
        )

        # Execute each tool Groq requested
        for tool_call in msg.tool_calls:
            name = tool_call.function.name
            args = (
                json.loads(tool_call.function.arguments)
                if tool_call.function.arguments
                else {}
            )
            print(f"[agent] → {name}({args if args else ''})")
            result = execute_tool(name, args)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result),
                }
            )

    return "I wasn't able to complete that. Please try again."


# ── Block Kit helpers ────────────────────────────────────────────────


def build_alert_blocks(today_str, expired, overdue, blocked):
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🤖 Casey's Daily Alert — {today_str}",
                "emoji": True,
            },
        },
        {"type": "divider"},
    ]

    if expired:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🚨 *Expired Contracts — {len(expired)} vendor(s)*",
                },
            }
        )
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "\n".join(
                        f"• *{v['name']}* — {v['deliverable']}" for v in expired
                    ),
                },
            }
        )
        blocks.append({"type": "divider"})

    if overdue:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"⚠️ *Overdue Deliverables — {len(overdue)} item(s)*",
                },
            }
        )
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(overdue)},
            }
        )
        blocks.append({"type": "divider"})

    if blocked:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🔴 *Blocked Tickets — {len(blocked)} ticket(s)*",
                },
            }
        )
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "\n".join(
                        f"• *{t['key']}* — {t['summary']} ({t['status']}, {t['priority']} priority)"
                        for t in blocked
                    ),
                },
            }
        )

    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "_Casey checks automatically every hour · Next check in ~60 min_",
                }
            ],
        }
    )

    return blocks


# ── Proactive alerts ─────────────────────────────────────────────────


def run_alerts():
    print("[alerts] Running alert check...")
    today = date.today()
    current_year = today.year

    vendors = _fetch_vendors()
    tickets = _fetch_tickets()

    expired = [v for v in vendors if v["status"] == "Expired"]

    overdue = []
    for v in vendors:
        if v["due_date"] and v["status"] != "Expired":
            try:
                due = datetime.strptime(
                    f"{v['due_date']} {current_year}", "%B %d %Y"
                ).date()
                if due < today:
                    overdue.append(
                        f"- {v['name']}: {v['deliverable']} (was due {v['due_date']})"
                    )
            except ValueError:
                pass

    blocked = [
        t
        for t in tickets
        if "blocked" in t["summary"].lower() or "blocked" in t["status"].lower()
    ]

    if any([expired, overdue, blocked]):
        blocks = build_alert_blocks(
            today.strftime("%B %d, %Y"), expired, overdue, blocked
        )
        app.client.chat_postMessage(
            channel=ALERT_CHANNEL,
            text=f"Casey's Daily Alert — {today.strftime('%B %d, %Y')}",
            blocks=blocks,
        )
        print(
            f"[alerts] Posted Block Kit alert ({len(expired)} expired, {len(overdue)} overdue, {len(blocked)} blocked)"
        )
    else:
        print("[alerts] All clear — nothing to report")


# ── Slack handlers ───────────────────────────────────────────────────


@app.message("hello")
def say_hello(message, say):
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"👋 Hey <@{message['user']}>, I'm Casey!",
                "emoji": True,
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "I'm your AI-powered BA assistant. I can answer questions about your "
                    "*vendors* and *sprint tickets* using real-time data.\n\n"
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
                {
                    "type": "mrkdwn",
                    "text": "💡 Type *alert now* to trigger an alert check",
                }
            ],
        },
    ]
    say(blocks=blocks, text=f"Hey <@{message['user']}>, I'm Casey!")


@app.message("alert now")
def handle_alert_now(message, say):
    say("Running alert check now... 🔍")
    run_alerts()
    say("Alert check complete! Check the alerts channel. ✅")


@app.message("")
def handle_message(message, say):
    text = message["text"]
    thread_ts = message.get("thread_ts", message["ts"])
    history = conversation_memory.get(thread_ts, [])

    say(text="Thinking... 🤔", thread_ts=thread_ts)
    answer = ask_casey(text, history)

    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": answer})
    conversation_memory[thread_ts] = history[-MAX_HISTORY:]

    say(text=answer, thread_ts=thread_ts)


# ── Startup ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_alerts, "interval", hours=1, id="alerts")
    scheduler.start()
    print("[scheduler] Alert job scheduled — runs every hour")

    run_alerts()

    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("Casey is running!")
    handler.start()
