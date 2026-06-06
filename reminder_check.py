"""
Fire due reminders: find reminders whose time has passed, send each to the
Telegram chat that saved it, then delete it so it doesn't repeat.

Run every ~15 minutes by launchd.  Manual test: python reminder_check.py
"""
import os
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import certifi
from dotenv import load_dotenv

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

import assistant_step4 as bot  # reuse the supabase client

SSL_CTX = ssl.create_default_context(cafile=certifi.where())
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")


def send(chat_id: str, text: str) -> None:
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
        r.read()


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set")

    now = datetime.now(timezone.utc)
    rows = bot.supabase.table("reminders").select("*").execute().data or []
    fired = 0
    for row in rows:
        try:
            dt = datetime.fromisoformat(str(row.get("remind_at")).replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt <= now:
            try:
                send(row["user_id"], f"⏰ Reminder: {row['text']}")
                bot.supabase.table("reminders").delete().eq("id", row["id"]).execute()
                fired += 1
            except Exception as e:
                print(f"[reminders] failed to fire {row.get('id')}: {e}")
    print(f"[reminders] {now.isoformat()} — checked {len(rows)}, fired {fired}")


if __name__ == "__main__":
    main()
