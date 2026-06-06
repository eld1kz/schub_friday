"""
Find your Telegram chat id so the daily job can message you.

Steps:
  1. Stop the bot (so it isn't consuming updates).
  2. Send any message to your bot in Telegram.
  3. Run:  python uni_get_chat_id.py
  4. Copy the printed id into .env as  TELEGRAM_CHAT_ID=...
"""
import json
import os
import ssl
import urllib.request
from pathlib import Path

import certifi
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
# Python.org's macOS build has no default CA store; use certifi's explicitly.
SSL_CTX = ssl.create_default_context(cafile=certifi.where())


def main() -> None:
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set in .env")
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    with urllib.request.urlopen(url, timeout=20, context=SSL_CTX) as r:
        data = json.loads(r.read())

    chats = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            who = chat.get("username") or chat.get("first_name") or "?"
            chats[chat["id"]] = who

    if not chats:
        print("No chats found. Stop the bot, send it a message, then run this again.")
        return
    print("Found chat id(s) — put the right one in .env as TELEGRAM_CHAT_ID:")
    for cid, who in chats.items():
        print(f"  TELEGRAM_CHAT_ID={cid}   ({who})")


if __name__ == "__main__":
    main()
