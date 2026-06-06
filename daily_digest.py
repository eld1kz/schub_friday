"""
Daily life briefing (выжимка). Gathers grades, assignments, calendar, email, and
reminders, then asks Claude to write a short summary and sends it to Telegram.

    python daily_digest.py            # MORNING: what's important / coming up
    python daily_digest.py --evening  # EVENING: what you achieved today

Scheduled twice a day by launchd. Needs TELEGRAM_CHAT_ID in .env to send;
without it, the briefing is just printed (handy for testing).
"""
import argparse
import json
import os
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import certifi
from dotenv import load_dotenv

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

import assistant_step4 as bot                     # calendar/gmail helpers, claude, supabase
import uni_daily                                  # refresh_session, diff, state
from uni_api_grades import fetch_grades, format_assignments

SSL_CTX = ssl.create_default_context(cafile=certifi.where())
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
KST = timezone(timedelta(hours=9))

MORNING_SYSTEM = (
    "You are the user's personal assistant writing a short MORNING briefing (a 'выжимка') "
    "sent to their Telegram. From the data provided, surface only what is important, new, or "
    "coming up today: new grades, assignments due soon or overdue, today's calendar, genuinely "
    "important unread emails, and due reminders. Use short scannable bullet lines with a few "
    "emojis. Skip anything empty or irrelevant — never pad. Under ~150 words. End with one "
    "brief encouraging line. Always write the briefing in English."
)
EVENING_SYSTEM = (
    "You are the user's personal assistant writing a short EVENING wrap-up (a 'выжимка') sent to "
    "their Telegram. Focus on what the user ACHIEVED today: newly posted grades and good results, "
    "assignments completed or submitted, events that happened. Warm, encouraging tone. Then "
    "briefly flag anything urgent due tomorrow. Short bullet lines, a few emojis, under ~150 words. "
    "Always write the briefing in English."
)


def send_telegram(text: str) -> None:
    if not (BOT_TOKEN and CHAT_ID):
        print("[digest] TELEGRAM_CHAT_ID not set — printed only, not sent.")
        return
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text[:4000]}).encode()
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
            r.read()
        print("[digest] sent to Telegram.")
    except Exception as e:
        print(f"[digest] Telegram send failed: {e}")


def notify_failure(reason: str) -> None:
    send_telegram(f"⚠️ Daily briefing problem:\n{reason}")


def gather(user_id: str) -> dict:
    parts: dict = {"grade_changes": []}
    # University: refresh session, fetch grades, diff vs last briefing, update baseline.
    try:
        uni_daily.refresh_session()
        results = fetch_grades()
        curr = uni_daily.flatten(results)
        prev = uni_daily.load_prev()
        parts["grade_changes"] = uni_daily.diff(prev, curr) if prev else []
        uni_daily.STATE_PATH.write_text(
            json.dumps(uni_daily.serialize(*curr), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        parts["assignments"] = format_assignments(results)
    except Exception as e:
        parts["uni_error"] = str(e)

    if user_id:
        try:
            parts["calendar"] = bot.list_upcoming_events(user_id, 8)
        except Exception as e:
            parts["calendar"] = f"(error: {e})"
        try:
            parts["email"] = bot.list_recent_emails(user_id, 8)
        except Exception as e:
            parts["email"] = f"(error: {e})"
        try:
            rows = bot.supabase.table("reminders").select("*").eq("user_id", user_id).execute()
            parts["reminders"] = rows.data or []
        except Exception:
            parts["reminders"] = []
    return parts


def build_context(parts: dict, when: str) -> str:
    today = datetime.now(KST).strftime("%A, %Y-%m-%d")
    lines = [f"Data for the user's {when} briefing. Today is {today} (KST).", ""]

    if parts.get("uni_error"):
        lines.append(f"UNIVERSITY: could not fetch ({parts['uni_error']}).")
    else:
        changes = parts.get("grade_changes") or []
        lines.append("GRADE/ASSIGNMENT CHANGES since last briefing:")
        lines += [f"  - {c}" for c in changes] if changes else ["  none"]
        lines += ["", "ASSIGNMENTS STATUS:", (parts.get("assignments", "") or "").strip()]

    lines += ["", "UPCOMING CALENDAR:", (parts.get("calendar", "") or "none").strip()]
    lines += ["", "RECENT EMAILS:", (parts.get("email", "") or "none").strip()]

    lines.append("")
    lines.append("SAVED REMINDERS:")
    rem = parts.get("reminders") or []
    lines += [f"  - {r.get('text')} @ {r.get('remind_at')}" for r in rem] if rem else ["  none"]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evening", action="store_true", help="evening achievements wrap-up")
    args = ap.parse_args()
    when = "evening" if args.evening else "morning"

    try:
        parts = gather(CHAT_ID)
        context = build_context(parts, when)
        system = EVENING_SYSTEM if args.evening else MORNING_SYSTEM

        resp = bot.claude.messages.create(
            model="claude-sonnet-4-6", max_tokens=700, system=system,
            messages=[{"role": "user", "content": context}],
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        print(text)
        send_telegram(text)

        # Loud alert if the university couldn't be read — otherwise grades go
        # stale silently (most likely a broken login / new 2FA prompt).
        if parts.get("uni_error"):
            notify_failure(
                "Couldn't read university data — grades/assignments may be stale. "
                "It may need a manual re-login (2FA?).\n"
                f"Details: {parts['uni_error'][:200]}"
            )
    except Exception as e:
        notify_failure(f"The {when} digest job crashed: {type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    main()
