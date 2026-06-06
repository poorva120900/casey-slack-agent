import os
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from dotenv import load_dotenv

load_dotenv(override=True, verbose=True)

app = App(token=os.environ["SLACK_BOT_TOKEN"])


@app.message("hello")
def say_hello(message, say):
    say(f"Hey there <@{message['user']}>! I'm Casey. How can I help?")


@app.message("")
def handle_message(message, say):
    say(f"You said: {message['text']} — I'm still learning, but I'll be smarter soon!")


if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("Casey is running...")
    handler.start()
