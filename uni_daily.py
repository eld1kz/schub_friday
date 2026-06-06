"""
Daily unattended university check:
  1. refresh the LMS session via headless login,
  2. fetch grades/assignments through the Canvas API,
  3. compare against the last run and Telegram-ping ONLY if something changed.

Run by launchd once a day. Manual test:  python uni_daily.py
Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env to send pings.
"""
import json
import os
import ssl
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import certifi
from dotenv import load_dotenv

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")
# Python.org's macOS build has no default CA store; use certifi's explicitly.
SSL_CTX = ssl.create_default_context(cafile=certifi.where())

from uni_api_grades import fetch_grades, _due_dt  # reuse the API reader

STATE_PATH = HERE / "uni_daily_state.json"
LOG_PATH = HERE / "uni_daily.log"
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat()}  {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def send_telegram(text: str) -> None:
    if not (BOT_TOKEN and CHAT_ID):
        log("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping notify.")
        return
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
            r.read()
        log("Telegram notify sent.")
    except Exception as e:
        log(f"Telegram notify failed: {e}")


def refresh_session() -> None:
    proc = subprocess.run(
        [sys.executable, str(HERE / "uni_refresh_session.py")],
        capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip()[:300])


def flatten(results: list[dict]):
    """Return (totals_by_course, {(course, assignment): item})."""
    totals, items = {}, {}
    for c in results:
        totals[c["short_name"]] = c["current_score"]
        for a in c["assignments"]:
            items[(c["short_name"], a["name"])] = a
    return totals, items


def diff(prev, curr) -> list[str]:
    p_tot, p_items = prev
    c_tot, c_items = curr
    changes = []
    for key, a in c_items.items():
        course, name = key
        old = p_items.get(key)
        if old is None:
            due = _due_dt(a["due_at"])
            due_txt = f" (due {due.strftime('%m-%d')})" if due else ""
            changes.append(f"🆕 New assignment — {course}: {name}{due_txt}")
        elif old.get("status") != "graded" and a.get("status") == "graded":
            changes.append(
                f"📊 Graded — {course}: {name} = {a.get('score')}/{a.get('points_possible')}"
            )
        elif a.get("status") == "graded" and old.get("score") != a.get("score"):
            changes.append(
                f"📊 Score changed — {course}: {name} {old.get('score')} → {a.get('score')}"
            )
    for course, tot in c_tot.items():
        if course in p_tot and p_tot[course] != tot:
            changes.append(f"📈 {course} total: {p_tot[course]} → {tot}")
    return changes


def serialize(totals, items) -> dict:
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "totals": totals,
        "items": {
            f"{course}|||{name}": {
                "status": v.get("status"), "score": v.get("score"),
                "points_possible": v.get("points_possible"), "due_at": v.get("due_at"),
            }
            for (course, name), v in items.items()
        },
    }


def load_prev():
    if not STATE_PATH.exists():
        return None
    raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    items = {}
    for k, v in raw.get("items", {}).items():
        course, name = k.split("|||", 1)
        items[(course, name)] = v
    return raw.get("totals", {}), items


def main() -> None:
    try:
        refresh_session()
        log("Session refreshed.")
    except Exception as e:
        log(f"Session refresh FAILED: {e}")
        send_telegram(f"⚠️ University auto-login failed — may need manual login (2FA?).\n{e}")
        return

    curr = flatten(fetch_grades())
    prev = load_prev()

    if prev is None:
        log("First run — saved baseline, no notify.")
    else:
        changes = diff(prev, curr)
        if changes:
            msg = "🎓 University updates:\n" + "\n".join("• " + c for c in changes[:20])
            send_telegram(msg)
            log(f"{len(changes)} change(s) notified.")
        else:
            log("No changes.")

    STATE_PATH.write_text(
        json.dumps(serialize(*curr), ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
