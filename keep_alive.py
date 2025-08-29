from flask import Flask
from threading import Thread
import os

app = Flask(__name__)

@app.get("/")
def home():
    return "Arcane City Bot is alive."

def run():
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

def start():
    Thread(target=run, daemon=True).start()
