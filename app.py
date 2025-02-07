import threading
from flask import Flask
import os
from bot import start_bot

app = Flask(__name__)

@app.route("/")
def index():
    return "Discord Bot is Running", 200

def run_flask():
    app.run(host="0.0.0.0", port=8080)

if __name__ == "__main__":
    # Start the Flask server in a separate thread so it respond gcp health checks
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()

    start_bot()