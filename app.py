import os
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from dotenv import load_dotenv
from notion_client import Client

load_dotenv(override=True)

app = App(token=os.environ["SLACK_BOT_TOKEN"])
notion = Client(auth=os.environ["NOTION_TOKEN"])
DATABASE_ID = os.environ["NOTION_DATABASE_ID"]


def search_vendors(query):
    # Fetch all vendors
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

        # Search across all fields
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
        return f"No results found for '{query}'"

    response = f"Found *{len(matched)}* result(s) for '*{query}*':\n\n"
    for v in matched:
        response += f"*{v['name']}*\n Status: {v['status']}\n Deliverable: {v['deliverable']}\n Due Date: {v['due_date']}\n Notes: {v['notes']}\n\n"

    return response


@app.message("hello")
def say_hello(message, say):
    say(
        f"Hey there <@{message['user']}>! I'm Casey. Ask me about any vendor, status, or deliverable!"
    )


@app.message("")
def handle_message(message, say):
    say(f"Searching for: *{message['text']}*...")
    result = search_vendors(message["text"])
    say(result)


if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("Casey is running...")
    handler.start()
