import os
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

import asyncio
import base64
import json
import re
import ssl
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from dotenv import load_dotenv
import anthropic
import certifi
import dateparser
try:
    from croniter import croniter
except ImportError:                       # schedule automations are skipped if missing
    croniter = None
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from supabase import create_client
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler,
    filters, ContextTypes,
)
from habit_llm_service import HabitLLMService
from habit_policy_service import HabitPolicyService
from habit_repository import SupabaseHabitRepository, iso, utcnow
from location_service import LocationService
from nudge_service import NudgeService
from places_provider import CachedPlacesProvider, GooglePlacesProvider
from retention_cleanup import cleanup_location_retention
from study_planner_service import StudyPlannerService
from study_repository import SupabaseStudyRepository
from health_repository import SupabaseHealthRepository
from health_service import HealthService
from readiness_service import ReadinessService
from workout_service import build as build_workout
from visit_detection_service import VisitDetectionService
from watcher_service import WatcherService
import weather

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
habit_repo = SupabaseHabitRepository(supabase)
habit_nudges = NudgeService(habit_repo, lambda uid, text, buttons=None: _send_telegram(uid, text, buttons))
habit_watchers = WatcherService(habit_repo, habit_nudges)
habit_policies = HabitPolicyService(habit_repo, habit_nudges)
habit_llm = HabitLLMService(habit_repo, claude_client=claude)
habit_location = LocationService(
    VisitDetectionService(
        habit_repo,
        CachedPlacesProvider(GooglePlacesProvider()),
        llm_service=habit_llm,
    ),
    habit_watchers,
    habit_policies,
)
study_planner = StudyPlannerService(SupabaseStudyRepository(supabase), claude)
health_service = HealthService(SupabaseHealthRepository(supabase))
readiness_service = ReadinessService(health_service.repo)

conversation_history: dict[str, deque] = {}
# Email drafts awaiting the user's yes/no confirmation before send_email sends.
pending_emails: dict[str, dict] = {}
# Action-tier automation runs awaiting an inline-keyboard confirm (id -> action).
pending_actions: dict[str, dict] = {}
# Calendar fast-path ops awaiting a Yes/No tap before the Google API is called.
pending_calendar: dict[str, dict] = {}
# Habit watcher drafts and destructive-data confirmations.
pending_habit_watchers: dict[str, dict] = {}
pending_forget: dict[str, str] = {}

# Only these Telegram user ids may use the bot. Falls back to TELEGRAM_CHAT_ID so
# it works with the existing config; empty set = locked (deny everyone).
ALLOWED_USER_IDS = {
    uid.strip()
    for uid in (os.environ.get("TELEGRAM_ALLOWED_IDS")
                or os.environ.get("TELEGRAM_CHAT_ID", "")).split(",")
    if uid.strip()
}


def _is_authorized(user_id: str) -> bool:
    return user_id in ALLOWED_USER_IDS


KST = timezone(timedelta(hours=9))


def _normalize_remind_at(value: str) -> str:
    """Treat a tz-naive remind_at (KST wall-clock) as KST, return a UTC ISO string."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(timezone.utc).isoformat()

# On Railway, set OAUTH_REDIRECT_URI to https://<your-app>.up.railway.app/oauth/callback
# and add that exact URL to the Google Cloud Console OAuth client. Falls back to
# localhost for local development.
REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:8080/oauth/callback")
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
SCOPES = CALENDAR_SCOPES + GMAIL_SCOPES
GOOGLE_CLIENT_CONFIG = {
    "web": {
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "redirect_uris": [REDIRECT_URI],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}


def _build_auth_url(user_id: str) -> str:
    """
    Build the Google OAuth consent URL. `state` carries the Telegram user_id
    through Google's redirect so the callback knows whose tokens to save.
    access_type=offline → returns a refresh_token; prompt=consent → always shows
    the consent screen so we get a fresh refresh_token.
    """
    flow = Flow.from_client_config(
        GOOGLE_CLIENT_CONFIG, scopes=SCOPES, redirect_uri=REDIRECT_URI,
        autogenerate_code_verifier=False,
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline", state=user_id, prompt="consent",
    )
    return auth_url


def _not_connected_msg(user_id: str, service: str) -> str:
    """Shown when a Google feature is used before the account is connected."""
    return (f"{service} isn't connected yet — you're still not connected. "
            f"Open this link to connect, then try again:\n\n{_build_auth_url(user_id)}")

BASE_SYSTEM_PROMPT = (
    "You are a concise, friendly personal assistant. "
    "Keep replies short and direct. No unnecessary filler. "
    "You have tools to check the time, save reminders, search the web, "
    "manage the user's Google Calendar, read and send Gmail, and check "
    "today's weather. "
    "When the user asks to send an email, call send_email to prepare a draft. "
    "It does NOT send immediately — show the user the drafted email and tell "
    "them to reply 'yes' to send or 'no' to cancel. "
    "You can also create and manage automations ('recipes') for the user. When "
    "they say 'create an automation: ...' (or describe a rule like 'every morning "
    "tell me X', 'when I get an email do Y'), call create_automation with their "
    "rule text verbatim and relay the confirmation. Use list_automations, "
    "set_automation, and delete_automation to manage them. You also proactively "
    "suggest automations; if the user asks to stop or resume those suggestions, "
    "call set_suggestions. For habit/location tracking, only create watcher rules "
    "when the user explicitly asks. Use neutral language; never diagnose health or "
    "claim one visit proves a problem. "
    "You are also a study/dev planner. When the user asks to plan their week or a "
    "course ('plan my week for OS + ML'), call create_study_plan; set "
    "include_resources true only if they want fresh resources/links. Relay the "
    "returned plan text verbatim — do not rewrite it. Use get_study_plan to show "
    "the plan, update_study_task for 'mark X as coded/done', log_study_result when "
    "a message starts with 'log:' or reports a study result, and study_reflection "
    "for 'review my week' / 'what did I learn'. "
    "For training and health, call get_health to pull the user's recent Apple "
    "Watch metrics and a readiness read (HRV, resting HR, respiratory rate, "
    "sleep, training load, VO2max). Use it when they ask how they're doing, "
    "whether to train hard today, or to plan a running/training session — base "
    "the plan on the readiness signal and recent load, and offer to schedule "
    "sessions via create_event or save_reminder. Give general guidance only; "
    "never diagnose, and suggest a doctor for anything concerning."
)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {"type": "web_search_20250305", "name": "web_search"},
    {
        "name": "get_current_datetime",
        "description": "Returns the current local date and time.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "save_reminder",
        "description": "Saves a reminder for the user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The reminder text"},
                "remind_at": {"type": "string", "description": "ISO 8601 datetime string"},
            },
            "required": ["text", "remind_at"],
        },
    },
    {
        "name": "list_upcoming_events",
        "description": "Lists the user's upcoming Google Calendar events.",
        "input_schema": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Number of events to return (default 5, max 10)",
                }
            },
            "required": [],
        },
    },
    {
        "name": "create_event",
        "description": "Creates a new event on the user's Google Calendar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Event title"},
                "start": {"type": "string", "description": "Start datetime in ISO 8601"},
                "end": {"type": "string", "description": "End datetime in ISO 8601"},
            },
            "required": ["title", "start", "end"],
        },
    },
    {
        "name": "list_recent_emails",
        "description": "Lists the user's most recent Gmail inbox messages (id, subject, sender).",
        "input_schema": {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": "Number of emails to return (default 5, max 10)",
                }
            },
            "required": [],
        },
    },
    {
        "name": "read_email",
        "description": "Returns the sender, subject, and body of one Gmail message by id. "
                       "Get the id from list_recent_emails first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The Gmail message id"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "send_email",
        "description": "Prepares an email to send via Gmail. Does NOT send immediately — the "
                       "draft is shown to the user, who must confirm before it is actually sent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Email subject"},
                "body": {"type": "string", "description": "Email body text"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "create_automation",
        "description": "Creates a user-defined automation ('recipe') from a plain-language rule, "
                       "e.g. 'every morning at 8 tell me my calendar', 'when I get an email from X "
                       "notify me', or 'when I text gym log a reminder'. Pass the user's "
                       "rule text verbatim; it is parsed into a trigger + action and saved.",
        "input_schema": {
            "type": "object",
            "properties": {
                "rule": {"type": "string", "description": "The plain-language automation rule"},
            },
            "required": ["rule"],
        },
    },
    {
        "name": "list_automations",
        "description": "Lists the user's saved automations with their id, on/off state, trigger, "
                       "and action.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_automation",
        "description": "Enables or disables one automation. Reference it by the id shown in "
                       "list_automations (a prefix is fine) or by a unique part of its description.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Automation id (or prefix) or description"},
                "enabled": {"type": "boolean", "description": "true to enable, false to disable"},
            },
            "required": ["ref", "enabled"],
        },
    },
    {
        "name": "delete_automation",
        "description": "Permanently deletes one automation. Reference it by id (prefix ok) or a "
                       "unique part of its description.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Automation id (or prefix) or description"},
            },
            "required": ["ref"],
        },
    },
    {
        "name": "set_suggestions",
        "description": "Turns the proactive automation SUGGESTIONS on or off. Use when the user "
                       "says things like 'stop suggesting automations' or 'suggest automations again'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "description": "true to allow suggestions, false to stop them"},
            },
            "required": ["enabled"],
        },
    },
    {
        "name": "create_habit_watcher",
        "description": "Creates an explicitly requested habit/location watcher after validating arguments.",
        "input_schema": {
            "type": "object",
            "properties": {
                "rule_type": {"type": "string"},
                "target_category": {"type": "string"},
                "target_brand": {"type": "string"},
                "threshold_count": {"type": "integer"},
                "window_days": {"type": "integer"},
                "reminder_text": {"type": "string"},
            },
            "required": ["rule_type"],
        },
    },
    {
        "name": "list_habit_watchers",
        "description": "Lists the user's active habit/location watcher rules.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "delete_habit_watcher",
        "description": "Deletes one habit watcher by id.",
        "input_schema": {
            "type": "object",
            "properties": {"watcher_id": {"type": "string"}},
            "required": ["watcher_id"],
        },
    },
    {
        "name": "set_habit_suggestions_enabled",
        "description": "Turns passive habit suggestions on or off.",
        "input_schema": {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
        },
    },
    {
        "name": "get_recent_place_visits",
        "description": "Returns recent confirmed place visits, without raw GPS coordinates.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
            "required": [],
        },
    },
    {
        "name": "get_weather",
        "description": "Returns today's Seoul forecast (current temp, high/low, rain "
                       "probability, UV) plus any contextual tips like bringing an umbrella. "
                       "Use when the user asks about the weather.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_health",
        "description": "Returns the user's recent Apple Health / Apple Watch metrics with "
                       "a readiness read: HRV, resting heart rate, respiratory rate and sleep "
                       "vs. their baseline, recent training load (steps, active energy, "
                       "exercise minutes, run distance), and VO2max/weight. Use when the user "
                       "asks how they're doing, whether to train hard, or to plan a training "
                       "session.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "create_study_plan",
        "description": "Generates and saves a personalized weekly study/dev plan from the "
                       "user's request (e.g. 'plan my week for OS + ML + side project'). "
                       "Injects the user's learning-profile insights and recent study logs. "
                       "Replaces the current active plan. Returns the formatted plan — show "
                       "it to the user verbatim.",
        "input_schema": {
            "type": "object",
            "properties": {
                "request": {"type": "string", "description": "The user's planning request, verbatim"},
                "include_resources": {
                    "type": "boolean",
                    "description": "true only if the user wants fresh resources/links researched via web search",
                },
            },
            "required": ["request"],
        },
    },
    {
        "name": "get_study_plan",
        "description": "Shows the user's current active study plan with each task's "
                       "pipeline status. Returns formatted text — show it verbatim.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "update_study_task",
        "description": "Moves one task in the active study plan to a new pipeline status. "
                       "Reference the task by a distinctive part of its title or course, "
                       "e.g. 'mutex' for 'mark OS mutex task as coded'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Part of the task title or course name"},
                "status": {
                    "type": "string",
                    "enum": ["idea", "researched", "coded", "tested", "reviewed", "done"],
                },
            },
            "required": ["ref", "status"],
        },
    },
    {
        "name": "log_study_result",
        "description": "Saves a study/dev log entry ('log: OS midterm 92%, Pomodoro worked "
                       "well'). Logs feed the learning loop that personalizes future plans. "
                       "Pass the user's log text verbatim.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The log text, verbatim"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "study_reflection",
        "description": "Analyzes recent study logs and task completion ('review my week', "
                       "'what did I learn last week?'): returns a summary and updates the "
                       "learning-profile insights used by future plans.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Lookback window in days (default 7)"},
            },
            "required": [],
        },
    },
]


# ---------------------------------------------------------------------------
# Google Calendar helpers
# ---------------------------------------------------------------------------

def _get_credentials(user_id: str) -> Credentials | None:
    """
    Load tokens from Supabase and return a valid Credentials object.
    Automatically refreshes the access_token if it's expired.
    Returns None if the user hasn't connected their calendar yet.
    """
    try:
        result = supabase.table("google_tokens").select("*").eq("user_id", user_id).execute()
        if not result.data:
            return None

        row = result.data[0]

        # Parse expiry. Supabase may return "+00:00" or bare ISO strings.
        # google-auth's .expired property uses naive utcnow(), so we store
        # a naive UTC datetime in creds.expiry.
        expiry = None
        if row.get("expiry"):
            dt = datetime.fromisoformat(row["expiry"].replace("Z", "+00:00"))
            expiry = dt.astimezone(timezone.utc).replace(tzinfo=None)

        creds = Credentials(
            token=row["access_token"],
            refresh_token=row["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            scopes=SCOPES,
        )
        if expiry:
            creds.expiry = expiry

        # If the access_token is expired, use the refresh_token to get a new one.
        # Request() is google-auth's HTTP transport for the refresh POST.
        if not creds.valid and creds.refresh_token:
            creds.refresh(Request())
            new_expiry = creds.expiry
            if new_expiry and new_expiry.tzinfo is None:
                new_expiry = new_expiry.replace(tzinfo=timezone.utc)
            supabase.table("google_tokens").update({
                "access_token": creds.token,
                "expiry": new_expiry.isoformat() if new_expiry else None,
            }).eq("user_id", user_id).execute()

        return creds
    except Exception as e:
        print(f"[calendar] credentials error: {type(e).__name__}: {e}")
        return None


def _calendar_service(user_id: str):
    """Return an authenticated Calendar API service object, or None."""
    creds = _get_credentials(user_id)
    if not creds:
        return None
    return build("calendar", "v3", credentials=creds)


def list_upcoming_events(user_id: str, max_results: int = 5) -> str:
    service = _calendar_service(user_id)
    if not service:
        return _not_connected_msg(user_id, "Google Calendar")
    try:
        now = datetime.now(timezone.utc).isoformat()
        result = service.events().list(
            calendarId="primary",
            timeMin=now,
            maxResults=min(max_results, 10),
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = result.get("items", [])
        if not events:
            return "No upcoming events."
        return "\n".join(
            f"• {e['summary']} — {e['start'].get('dateTime', e['start'].get('date', '?'))}"
            for e in events
        )
    except Exception as e:
        return f"Error fetching events: {e}"


def create_calendar_event(user_id: str, title: str, start: str, end: str) -> str:
    service = _calendar_service(user_id)
    if not service:
        return _not_connected_msg(user_id, "Google Calendar")
    try:
        event = {
            "summary": title,
            "start": {"dateTime": start, "timeZone": "UTC"},
            "end":   {"dateTime": end,   "timeZone": "UTC"},
        }
        created = service.events().insert(calendarId="primary", body=event).execute()
        start_str = created["start"].get("dateTime", "?")
        return f"Event created: '{created['summary']}' at {start_str}"
    except Exception as e:
        return f"Error creating event: {e}"


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def _gmail_service(user_id: str):
    """Return an authenticated Gmail API service object, or None."""
    creds = _get_credentials(user_id)
    if not creds:
        return None
    return build("gmail", "v1", credentials=creds)


def _decode_body(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")


def _extract_body(payload: dict) -> str:
    """Pull the plain-text body out of a Gmail message payload (handles multipart)."""
    if payload.get("body", {}).get("data"):
        return _decode_body(payload["body"]["data"])
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return _decode_body(part["body"]["data"])
    for part in payload.get("parts", []):       # nested multipart/alternative
        nested = _extract_body(part)
        if nested and nested != "(no plain-text body)":
            return nested
    return "(no plain-text body)"


def list_recent_emails(user_id: str, count: int = 5) -> str:
    service = _gmail_service(user_id)
    if not service:
        return _not_connected_msg(user_id, "Gmail")
    try:
        resp = service.users().messages().list(
            userId="me", maxResults=min(count, 10), labelIds=["INBOX"]
        ).execute()
        msgs = resp.get("messages", [])
        if not msgs:
            return "No messages found."
        lines = []
        for m in msgs:
            full = service.users().messages().get(
                userId="me", id=m["id"], format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()
            headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
            sender = headers.get("From", "?")
            subject = headers.get("Subject", "(no subject)")
            lines.append(f"• [{m['id']}] {subject} — {sender}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing emails: {e}"


def read_email(user_id: str, message_id: str) -> str:
    service = _gmail_service(user_id)
    if not service:
        return _not_connected_msg(user_id, "Gmail")
    try:
        msg = service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
        payload = msg["payload"]
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
        sender = headers.get("From", "?")
        subject = headers.get("Subject", "(no subject)")
        body = _extract_body(payload).strip()[:3000]
        return f"From: {sender}\nSubject: {subject}\n\n{body}"
    except Exception as e:
        return f"Error reading email: {e}"


def _send_email_now(user_id: str, to: str, subject: str, body: str) -> str:
    """Actually send an email. Called only after the user confirms."""
    service = _gmail_service(user_id)
    if not service:
        return _not_connected_msg(user_id, "Gmail")
    try:
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        # Don't trust "no exception" alone — verify Gmail actually filed it under SENT.
        msg_id = sent.get("id")
        labels = sent.get("labelIds", [])
        if not msg_id or "SENT" not in labels:
            return (
                f"⚠️ Gmail accepted the request but didn't confirm it as sent "
                f"(id={msg_id}, labels={labels}). Check your Sent folder."
            )
        return f"✅ Sent to {to} (Gmail id {msg_id})"
    except Exception as e:
        return f"Error sending email: {e}"


def _classify_confirmation(text: str) -> str:
    """Map a follow-up message to 'send', 'cancel', or 'unclear'. Biased toward not sending."""
    t = text.strip().lower()
    if t in {"no", "n", "cancel", "stop", "nevermind", "never mind", "don't", "do not"} \
            or t.startswith(("no ", "cancel", "don't", "do not", "stop", "nevermind")):
        return "cancel"
    if t in {"yes", "y", "send", "send it", "confirm", "ok", "okay", "yep", "yeah", "sure"} \
            or t.startswith(("yes", "send", "confirm", "go ahead", "do it")):
        return "send"
    return "unclear"


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def run_tool(name: str, tool_input: dict, user_id: str) -> str:
    if name == "get_current_datetime":
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if name == "save_reminder":
        try:
            supabase.table("reminders").insert({
                "user_id": user_id,
                "text": tool_input["text"],
                "remind_at": _normalize_remind_at(tool_input["remind_at"]),
            }).execute()
            return f"Saved: '{tool_input['text']}' for {tool_input['remind_at']}"
        except Exception as e:
            return f"Failed to save reminder: {e}"

    if name == "list_upcoming_events":
        return list_upcoming_events(user_id, tool_input.get("max_results", 5))

    if name == "create_event":
        return create_calendar_event(
            user_id, tool_input["title"], tool_input["start"], tool_input["end"]
        )

    if name == "list_recent_emails":
        return list_recent_emails(user_id, tool_input.get("count", 5))

    if name == "read_email":
        return read_email(user_id, tool_input["id"])

    if name == "send_message":            # used by automations to notify the user
        return tool_input.get("text", "")

    if name == "create_automation":
        return create_automation(user_id, tool_input["rule"])

    if name == "list_automations":
        return list_automations(user_id)

    if name == "set_automation":
        return set_automation_enabled(user_id, tool_input["ref"], tool_input["enabled"])

    if name == "delete_automation":
        return delete_automation(user_id, tool_input["ref"])

    if name == "set_suggestions":
        return set_suggestions(user_id, tool_input["enabled"])

    if name == "create_habit_watcher":
        try:
            row = habit_watchers.create_habit_watcher(
                user_id=user_id,
                rule_type=tool_input["rule_type"],
                target_category=tool_input.get("target_category"),
                target_brand=tool_input.get("target_brand"),
                threshold_count=tool_input.get("threshold_count"),
                window_days=tool_input.get("window_days"),
                reminder_text=tool_input.get("reminder_text"),
            )
            target = row.get("target_brand") or row.get("target_category") or "that pattern"
            if row["rule_type"] == "weekly_visit_limit":
                return (
                    f"Got it. I'll let you know when you exceed "
                    f"{row.get('threshold_count')} {str(target).replace('_', ' ')} visits during a calendar week."
                )
            if row["rule_type"].startswith("near_"):
                return f"Got it. I'll remind you when you are near {str(target).replace('_', ' ')}."
            return f"Saved habit watcher {str(row.get('id', ''))[:8]}."
        except Exception as e:
            return f"Could not create that watcher: {e}"

    if name == "list_habit_watchers":
        rows = habit_watchers.list_habit_watchers(user_id)
        if not rows:
            return "No active habit watchers."
        return "\n".join(
            f"[{str(r['id'])[:8]}] {r['rule_type']} — "
            f"{r.get('target_brand') or r.get('target_category') or r.get('target_place_id') or 'target'}"
            for r in rows
        )

    if name == "delete_habit_watcher":
        ok = habit_watchers.delete_habit_watcher(user_id, tool_input["watcher_id"])
        return "Deleted watcher." if ok else "No watcher matched that id."

    if name == "set_habit_suggestions_enabled":
        habit_repo.update_settings(user_id, habit_suggestions_enabled=tool_input["enabled"])
        return "Passive habit suggestions are on." if tool_input["enabled"] else "Passive habit suggestions are off."

    if name == "get_recent_place_visits":
        rows = habit_repo.list_visits(user_id, min(int(tool_input.get("limit", 20)), 20))
        if not rows:
            return "No confirmed visits yet."
        return "\n".join(
            f"{r['place_name']} — {r['normalized_category']} — {r['confirmed_at']}"
            for r in rows
        )

    if name == "get_weather":
        return weather.weather_block()

    if name == "get_health":
        status = readiness_service.check(user_id)
        workout = build_workout(status)
        header = (f"{status['summary']}\n"
                  f"Today's session: {workout['summary']}")
        return header + "\n\n" + health_service.status_summary(user_id)

    if name == "create_study_plan":
        try:
            return study_planner.generate_plan(
                user_id, tool_input["request"],
                extra_context=fetch_memories(user_id),
                include_resources=tool_input.get("include_resources", False),
            )
        except Exception as e:
            return f"Could not generate the plan: {e}"

    if name == "get_study_plan":
        return study_planner.show_current_plan(user_id)

    if name == "update_study_task":
        return study_planner.update_task_status(user_id, tool_input["ref"], tool_input["status"])

    if name == "log_study_result":
        return study_planner.log_result(user_id, tool_input["text"])

    if name == "study_reflection":
        try:
            return study_planner.reflect(user_id, days=int(tool_input.get("days", 7)))
        except Exception as e:
            return f"Could not run the reflection: {e}"

    if name == "send_email":
        # Do NOT send here. Stash the draft; the user confirms in their next message.
        pending_emails[user_id] = {
            "to": tool_input["to"],
            "subject": tool_input["subject"],
            "body": tool_input["body"],
        }
        return (
            "Draft saved but NOT sent yet. Show the user this exact draft and ask "
            "them to reply 'yes' to send or 'no' to cancel.\n"
            f"To: {tool_input['to']}\n"
            f"Subject: {tool_input['subject']}\n\n"
            f"{tool_input['body']}"
        )

    return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# Tool-use loop (unchanged from step 3)
# ---------------------------------------------------------------------------

def route_model(user_message: str) -> str:
    """
    Classify the message and pick a model. Costs one cheap Haiku call per message.
    Returns a model id; defaults to Haiku if the classifier misbehaves.
    """
    instruction = (
        "Classify this user message into one of two labels:\n"
        "- simple  : casual chat, short factual question, basic tool\n"
        "            request (calendar, reminder, search), or anything\n"
        "            routine\n"
        "- complex : multi-step reasoning, planning, careful drafting,\n"
        "            long-context work, judgment calls, or anything\n"
        "            requiring nuance\n"
        "Reply with ONLY one word: simple OR complex."
    )
    try:
        response = claude.messages.create(
            model="claude-haiku-4-5",
            max_tokens=5,
            system=instruction,
            messages=[{"role": "user", "content": user_message}],
        )
        label = response.content[0].text.strip().lower()
    except Exception as e:
        print(f"[route] classifier error: {type(e).__name__}: {e} -> defaulting simple")
        label = "simple"

    if label not in ("simple", "complex"):
        label = "simple"

    model = "claude-sonnet-4-6" if label == "complex" else "claude-haiku-4-5"
    print(f"[route] {label} -> {model}")
    return model


def call_claude(messages: list, system: str, user_id: str, model: str,
                on_tool=None) -> str:
    while True:
        response = claude.messages.create(
            model=model,
            max_tokens=1024,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        # Surface server-side tools (e.g. web_search) to the live status message.
        if on_tool:
            for block in response.content:
                if getattr(block, "type", None) == "server_tool_use":
                    on_tool(block.name)

        if response.stop_reason in ("end_turn", "max_tokens"):
            return "".join(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                if on_tool:
                    on_tool(block.name)
                result = run_tool(block.name, block.input, user_id)
                _log_activity(user_id, block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
            messages.append({"role": "user", "content": tool_results})


# ---------------------------------------------------------------------------
# Memory helpers (unchanged from step 3)
# ---------------------------------------------------------------------------

def fetch_memories(user_id: str) -> str:
    try:
        result = (
            supabase.table("memories")
            .select("content")
            .eq("user_id", user_id)
            .execute()
        )
        if not result.data:
            return ""
        facts = "\n".join(f"- {row['content']}" for row in result.data)
        return f"\nKnown facts about this user:\n{facts}"
    except Exception as e:
        print(f"[memory] fetch error: {type(e).__name__}: {e}")
        return ""


def maybe_save_memory(user_id: str, user_text: str, assistant_reply: str) -> None:
    check = (
        f"User said: {user_text}\n"
        f"Assistant replied: {assistant_reply}\n\n"
        "Is there a durable fact or preference about the user worth remembering "
        "(e.g. name, job, hobby, preference)? "
        "If yes, reply with one short sentence starting with 'The user'. "
        "If no, reply exactly: NONE"
    )
    try:
        response = claude.messages.create(
            model="claude-haiku-4-5",
            max_tokens=80,
            messages=[{"role": "user", "content": check}],
        )
        fact = response.content[0].text.strip().rstrip(".")
        if fact.upper() == "NONE":
            return
        supabase.table("memories").insert(
            {"user_id": user_id, "content": fact}
        ).execute()
    except Exception as e:
        print(f"[memory] save error: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Live "work in progress" feedback
# ---------------------------------------------------------------------------

# Status text shown while each tool runs.
TOOL_STATUS = {
    "get_current_datetime": "🕒 Checking the time...",
    "save_reminder": "⏰ Saving reminder...",
    "web_search": "🔍 Searching the web...",
    "list_upcoming_events": "📅 Reading your calendar...",
    "create_event": "📅 Adding to your calendar...",
    "list_recent_emails": "✉️ Checking your inbox...",
    "read_email": "✉️ Reading the email...",
    "send_email": "📤 Preparing the draft...",
    "get_weather": "🌤 Checking the weather...",
    "create_study_plan": "📚 Building your study plan...",
    "get_study_plan": "📚 Fetching your plan...",
    "update_study_task": "✅ Updating the task...",
    "log_study_result": "📓 Saving your log...",
    "study_reflection": "🪞 Reviewing your week...",
}


class StatusReporter:
    """
    Live feedback for one incoming message:
      • refreshes the Telegram typing indicator every 4s, and
      • shows/edits a single status message ('🤔 Thinking...', then per phase).

    The status bubble is created lazily after SHOW_DELAY, so fast replies
    don't flicker it. finish() cancels everything and removes the bubble.
    """
    SHOW_DELAY = 1.0       # don't show the bubble until work has taken this long
    TYPING_EVERY = 4.0     # typing action expires after ~5s, so refresh sooner

    def __init__(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
        self.context = context
        self.chat_id = chat_id
        self.loop = asyncio.get_running_loop()
        self.text = "🤔 Thinking..."
        self.message = None
        self._typing_task = None
        self._show_task = None
        self._lock = asyncio.Lock()
        self._done = False

    async def start(self) -> None:
        self._typing_task = asyncio.create_task(self._keep_typing())
        self._show_task = asyncio.create_task(self._show_after_delay())

    async def _keep_typing(self) -> None:
        try:
            while True:
                await self.context.bot.send_chat_action(self.chat_id, "typing")
                await asyncio.sleep(self.TYPING_EVERY)
        except asyncio.CancelledError:
            pass

    async def _show_after_delay(self) -> None:
        try:
            await asyncio.sleep(self.SHOW_DELAY)
            async with self._lock:
                if not self._done and self.message is None:
                    self.message = await self.context.bot.send_message(
                        self.chat_id, self.text
                    )
        except asyncio.CancelledError:
            pass

    async def update(self, text: str) -> None:
        """Set the current phase; edits the bubble if it's already shown."""
        async with self._lock:
            self.text = text
            if self.message is not None and not self._done:
                try:
                    await self.context.bot.edit_message_text(
                        text, chat_id=self.chat_id,
                        message_id=self.message.message_id,
                    )
                except Exception:
                    pass

    def update_threadsafe(self, text: str) -> None:
        """update() callable from the worker thread running call_claude."""
        asyncio.run_coroutine_threadsafe(self.update(text), self.loop)

    async def finish(self) -> None:
        """Stop typing, cancel the pending bubble, and delete it if shown."""
        self._done = True
        for task in (self._typing_task, self._show_task):
            if task:
                task.cancel()
        async with self._lock:
            if self.message is not None:
                try:
                    await self.context.bot.delete_message(
                        self.chat_id, self.message.message_id
                    )
                except Exception:
                    pass
                self.message = None


# ---------------------------------------------------------------------------
# Calendar fast-path — cheap, reliable add/edit/delete that skips the tool loop
#
# Why: routing "add dinner tomorrow 7pm" through the full loop spends a big
# system prompt (full tool catalog + memory + history) AND makes the model do
# date math, which it gets wrong. The fast-path instead: a free regex gate, then
# ONE tiny Haiku call that only extracts wording (no date math), then dateparser
# resolves the time exactly against KST, then a TEMPLATED Yes/No confirm before
# the Google API is ever called. Anything it can't cleanly handle returns False
# and falls through to the unchanged full tool loop.
# ---------------------------------------------------------------------------

# Free pre-filter: a calendar verb near a time word, or an explicit calendar noun.
_CAL_VERB = (r"add|schedule|create|set ?up|book|move|reschedul\w*|shift|push|"
             r"change|cancel|delete|remove|put")
_CAL_TIME = (r"tomorrow|tonight|today|next |this |on \w+day|\bat \d|\d\s?(am|pm)\b|"
             r"\d:\d\d|\bmon|tue|wed|thu|fri|sat|sun|"
             r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec")
_CAL_NOUN = r"\b(calendar|meeting|appointment)\b"
_CAL_VERB_RE = re.compile(rf"\b({_CAL_VERB})\b", re.I)
_CAL_TIME_RE = re.compile(_CAL_TIME, re.I)
_CAL_NOUN_RE = re.compile(_CAL_NOUN, re.I)


def _cal_gate(text: str) -> bool:
    return bool(_CAL_NOUN_RE.search(text)
                or (_CAL_VERB_RE.search(text) and _CAL_TIME_RE.search(text)))


_CAL_EXTRACT_SYSTEM = (
    "Extract ONE calendar operation from the user's message as strict JSON with keys: "
    "action ('add'|'edit'|'delete'|'none'), title, time_phrase, end_phrase, "
    "duration_minutes, recurrence, event_reference.\n"
    "Rules:\n"
    "- Return action 'none' if it is NOT a simple calendar add/edit/delete (a reminder, "
    "a question, listing events, or anything complex/ambiguous).\n"
    "- Copy the user's date/time wording VERBATIM into time_phrase/end_phrase. Do NOT "
    "compute, resolve, or reformat dates.\n"
    "- For edit/delete, put the words identifying which event into event_reference.\n"
    "- recurrence: a short word like 'daily'/'weekly'/'every monday', else null.\n"
    "- Use null for any missing field. Output ONLY the JSON object."
)


def _extract_calendar(text: str) -> dict | None:
    """One minimal Haiku call: message -> structured calendar op (no date math)."""
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5", max_tokens=200,
            system=_CAL_EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": text}],
        )
        raw = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        if "{" in raw and "}" in raw:
            raw = raw[raw.find("{"): raw.rfind("}") + 1]
        return json.loads(raw)
    except Exception as e:
        print(f"[cal] extract failed: {e}")
        return None


def _to_rrule(recurrence) -> str | None:
    if not recurrence:
        return None
    r = str(recurrence).lower()
    if r.startswith("rrule"):
        return recurrence
    table = {
        "weekday": "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
        "daily": "RRULE:FREQ=DAILY", "every day": "RRULE:FREQ=DAILY",
        "weekly": "RRULE:FREQ=WEEKLY", "every week": "RRULE:FREQ=WEEKLY",
        "monthly": "RRULE:FREQ=MONTHLY", "every month": "RRULE:FREQ=MONTHLY",
        "yearly": "RRULE:FREQ=YEARLY",
    }
    for k, v in table.items():
        if k in r:
            return v
    days = {"monday": "MO", "tuesday": "TU", "wednesday": "WE", "thursday": "TH",
            "friday": "FR", "saturday": "SA", "sunday": "SU"}
    for d, code in days.items():
        if d in r:
            return f"RRULE:FREQ=WEEKLY;BYDAY={code}"
    return None


_NEXT_WEEKDAY_RE = re.compile(
    r"\b(?:next|this)\s+(mon|tue|wed|thu|fri|sat|sun)", re.I)


def _dp(phrase: str, base: datetime):
    """Parse a natural date/time phrase as KST, anchored to `base` (naive KST)."""
    # dateparser returns None for "next/this <weekday>"; drop the qualifier and
    # let PREFER_DATES_FROM=future resolve the upcoming weekday. It also doesn't
    # know "tonight" — treat it as today (the time part carries the hour).
    phrase = _NEXT_WEEKDAY_RE.sub(r"\1", phrase or "")
    phrase = re.sub(r"\btonight\b", "today", phrase, flags=re.I)
    return dateparser.parse(phrase, settings={
        "TIMEZONE": "Asia/Seoul",
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": base,
    })


def _resolve_times(spec: dict):
    """Turn time_phrase/end_phrase/duration into exact KST start/end. dateparser
    does ALL date math, so relatives ('next Friday', 'tomorrow 7pm') are exact."""
    now_kst = datetime.now(KST).replace(tzinfo=None)
    start = _dp(spec.get("time_phrase") or "", now_kst)
    if not start:
        return None, None, None
    end = None
    if spec.get("end_phrase"):
        parsed_end = _dp(spec["end_phrase"], start.replace(tzinfo=None))
        if parsed_end:
            # A range like "7pm–8pm" gives a time-only end: pin it to the start's
            # date (rolling past midnight if needed) rather than trust its date.
            end = start.replace(hour=parsed_end.hour, minute=parsed_end.minute,
                                second=0, microsecond=0)
            if end <= start:
                end += timedelta(days=1)
    if not end:
        try:
            dur = int(spec.get("duration_minutes") or 60)
        except (TypeError, ValueError):
            dur = 60
        end = start + timedelta(minutes=dur)
    return start, end, _to_rrule(spec.get("recurrence"))


def _t(dt: datetime) -> str:
    return dt.strftime("%-I:%M%p").lower()           # e.g. 7:00pm


def _fmt_range(start: datetime, end: datetime) -> str:
    if start.date() == end.date():
        return f"{start.strftime('%a %b %-d')}, {_t(start)}–{_t(end)}"
    return f"{start.strftime('%a %b %-d')} {_t(start)} – {end.strftime('%a %b %-d')} {_t(end)}"


def _event_range(ev: dict) -> str:
    """Human range for an existing Google event."""
    s = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date")
    e = ev.get("end", {}).get("dateTime") or ev.get("end", {}).get("date")
    try:
        sd = datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(KST)
        ed = datetime.fromisoformat(e.replace("Z", "+00:00")).astimezone(KST)
        return _fmt_range(sd, ed)
    except Exception:
        return s or "?"


def _search_events(user_id: str, reference: str, max_results: int = 5):
    """Search upcoming events matching a reference. None = not connected."""
    service = _calendar_service(user_id)
    if not service:
        return None
    try:
        time_min = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        result = service.events().list(
            calendarId="primary", q=reference, timeMin=time_min,
            maxResults=max_results, singleEvents=True, orderBy="startTime",
        ).execute()
        return result.get("items", [])
    except Exception as e:
        print(f"[cal] search failed: {e}")
        return []


_CAL_BTNS = lambda op_id: [("✅ Yes", f"cal:yes:{op_id}"), ("❌ No", f"cal:no:{op_id}")]


def try_calendar_fastpath(user_id: str, text: str) -> bool:
    """Handle a simple calendar add/edit/delete. Returns True if handled (a
    confirm was sent or a clear message given); False to fall through to the
    full tool loop. Blocking — call via asyncio.to_thread."""
    if not _cal_gate(text):
        return False
    spec = _extract_calendar(text)
    if not spec or spec.get("action") not in ("add", "edit", "delete"):
        return False
    action = spec["action"]

    if action == "add":
        start, end, rrule = _resolve_times(spec)
        if not start:
            return False                      # couldn't parse → let the full loop try
        title = (spec.get("title") or "Event").strip()
        op_id = uuid.uuid4().hex
        pending_calendar[op_id] = {
            "user_id": user_id, "type": "add", "title": title,
            "start": start, "end": end, "rrule": rrule,
            "summary": f"{title} — {_fmt_range(start, end)}",
        }
        rec = f" (repeats {spec.get('recurrence')})" if rrule else ""
        _send_telegram(user_id, f"📅 {title} — {_fmt_range(start, end)}.{rec}\nAdd it?",
                       buttons=_CAL_BTNS(op_id))
        return True

    # edit / delete: find the event first, then confirm the match.
    ref = (spec.get("event_reference") or spec.get("title") or "").strip()
    if not ref:
        return False
    matches = _search_events(user_id, ref)
    if matches is None:
        _send_telegram(user_id, _not_connected_msg(user_id, "Google Calendar"))
        return True
    if not matches:
        _send_telegram(user_id, f"Couldn't find a calendar event matching '{ref}'.")
        return True
    ev = matches[0]
    summary = ev.get("summary", "(untitled)")
    extra = f"\n(+{len(matches) - 1} other match — tap No to cancel)" if len(matches) > 1 else ""
    op_id = uuid.uuid4().hex

    if action == "delete":
        pending_calendar[op_id] = {
            "user_id": user_id, "type": "delete", "event_id": ev["id"],
            "summary": f"{summary} — {_event_range(ev)}",
        }
        _send_telegram(user_id, f"🗑️ Delete '{summary}' — {_event_range(ev)}?{extra}",
                       buttons=_CAL_BTNS(op_id))
        return True

    # edit = move to a new time
    start, end, _ = _resolve_times(spec)
    if not start:
        _send_telegram(user_id, f"Found '{summary}', but couldn't parse the new time. "
                                "Try e.g. 'move it to Friday 3pm'.")
        return True
    pending_calendar[op_id] = {
        "user_id": user_id, "type": "edit", "event_id": ev["id"],
        "start": start, "end": end,
        "summary": f"{summary} → {_fmt_range(start, end)}",
    }
    _send_telegram(
        user_id,
        f"📅 Move '{summary}'\nfrom {_event_range(ev)}\nto {_fmt_range(start, end)}?{extra}",
        buttons=_CAL_BTNS(op_id),
    )
    return True


def commit_calendar_op(user_id: str, op_id: str) -> str:
    """Called only after a Yes tap. Performs the Google API write."""
    op = pending_calendar.pop(op_id, None)
    if not op or op.get("user_id") != user_id:
        return "That calendar request has expired."
    service = _calendar_service(user_id)
    if not service:
        return _not_connected_msg(user_id, "Google Calendar")
    try:
        if op["type"] == "add":
            body = {
                "summary": op["title"],
                "start": {"dateTime": op["start"].isoformat(), "timeZone": "Asia/Seoul"},
                "end": {"dateTime": op["end"].isoformat(), "timeZone": "Asia/Seoul"},
            }
            if op.get("rrule"):
                body["recurrence"] = [op["rrule"]]
            service.events().insert(calendarId="primary", body=body).execute()
            return f"✅ Added: {op['summary']}"
        if op["type"] == "edit":
            service.events().patch(
                calendarId="primary", eventId=op["event_id"],
                body={
                    "start": {"dateTime": op["start"].isoformat(), "timeZone": "Asia/Seoul"},
                    "end": {"dateTime": op["end"].isoformat(), "timeZone": "Asia/Seoul"},
                },
            ).execute()
            return f"✅ Moved: {op['summary']}"
        if op["type"] == "delete":
            service.events().delete(calendarId="primary", eventId=op["event_id"]).execute()
            return f"🗑️ Deleted: {op['summary']}"
    except Exception as e:
        return f"Calendar error: {e}"
    return "Unknown calendar operation."


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def _authorized_command(update: Update) -> str | None:
    user_id = str(update.effective_user.id)
    if not _is_authorized(user_id):
        await update.message.reply_text("Sorry, this is a private bot.")
        return None
    return user_id


async def handle_geoloc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized_command(update):
        return
    await update.message.reply_text(
        "Open the attachment menu in Telegram, choose Location, and select Share My Live Location. "
        "I only receive updates while you actively share your live location. You can stop sharing "
        "in Telegram at any time or send /location_off."
    )


async def handle_location_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = await _authorized_command(update)
    if not user_id:
        return
    s = await asyncio.to_thread(habit_repo.ensure_settings, user_id)
    last = s.get("last_location_at") or "never"
    state = "on" if s.get("tracking_enabled", True) else "off"
    await update.message.reply_text(f"Location processing is {state}. Last location update: {last}.")


async def handle_location_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = await _authorized_command(update)
    if not user_id:
        return
    await asyncio.to_thread(habit_repo.update_settings, user_id, tracking_enabled=False)
    await update.message.reply_text("Location processing is off. Telegram live sharing can also be stopped in Telegram.")


async def handle_nudges_setting(update: Update, context: ContextTypes.DEFAULT_TYPE, enabled: bool) -> None:
    user_id = await _authorized_command(update)
    if not user_id:
        return
    await asyncio.to_thread(habit_repo.update_settings, user_id, nudges_enabled=enabled)
    await update.message.reply_text("Proactive notifications are on." if enabled else "Proactive notifications are off.")


async def handle_nudges_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_nudges_setting(update, context, True)


async def handle_nudges_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_nudges_setting(update, context, False)


async def handle_nudges_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = await _authorized_command(update)
    if not user_id:
        return
    s = await asyncio.to_thread(habit_repo.ensure_settings, user_id)
    today = await asyncio.to_thread(habit_repo.nudges_today, user_id, habit_nudges._day_start(s.get("timezone") or "Asia/Seoul"))
    await update.message.reply_text(
        f"Nudges: {'on' if s.get('nudges_enabled', True) else 'off'}\n"
        f"Passive habit suggestions: {'on' if s.get('habit_suggestions_enabled', True) else 'off'}\n"
        f"Daily limit: {s.get('daily_nudge_limit') or 3}\n"
        f"Sent today: {today}"
    )


async def handle_visits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = await _authorized_command(update)
    if not user_id:
        return
    rows = await asyncio.to_thread(habit_repo.list_visits, user_id, 10)
    if not rows:
        await update.message.reply_text("No confirmed visits yet.")
        return
    lines = [
        f"[{str(r['id'])[:8]}] {r['place_name']} — {r['normalized_category']} — {r['confirmed_at']}"
        for r in rows
    ]
    await update.message.reply_text("\n".join(lines))


async def handle_habit_watchers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = await _authorized_command(update)
    if not user_id:
        return
    rows = await asyncio.to_thread(habit_watchers.list_habit_watchers, user_id)
    if not rows:
        await update.message.reply_text("No active habit watchers.")
        return
    lines = []
    for r in rows:
        target = r.get("target_brand") or r.get("target_category") or r.get("target_place_id") or "target"
        lines.append(f"[{str(r['id'])[:8]}] {r['rule_type']} — {target}")
    await update.message.reply_text("\n".join(lines))


async def handle_habit_suggestions_setting(update: Update, context: ContextTypes.DEFAULT_TYPE, enabled: bool) -> None:
    user_id = await _authorized_command(update)
    if not user_id:
        return
    await asyncio.to_thread(habit_repo.update_settings, user_id, habit_suggestions_enabled=enabled)
    await update.message.reply_text(
        "Passive habit suggestions are on." if enabled else "Passive habit suggestions are off."
    )


async def handle_habit_suggestions_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_habit_suggestions_setting(update, context, True)


async def handle_habit_suggestions_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_habit_suggestions_setting(update, context, False)


async def handle_delete_watcher(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = await _authorized_command(update)
    if not user_id:
        return
    if not context.args:
        await update.message.reply_text("Use /delete_watcher <id>.")
        return
    ok = await asyncio.to_thread(habit_watchers.delete_habit_watcher, user_id, context.args[0])
    await update.message.reply_text("Deleted watcher." if ok else "No watcher matched that id.")


async def handle_forget_locations(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = await _authorized_command(update)
    if not user_id:
        return
    pending_forget[user_id] = "locations"
    await update.message.reply_text("This will delete stored location updates, candidates, and visits. Reply 'yes' to confirm.")


async def handle_forget_habits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = await _authorized_command(update)
    if not user_id:
        return
    pending_forget[user_id] = "habits"
    await update.message.reply_text("This will delete habit rules, suggestions, and related nudge history. Reply 'yes' to confirm.")


async def handle_location_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    if not _is_authorized(user_id):
        print(f"[auth] blocked location from user {user_id}")
        return
    msg = update.edited_message or update.message
    if not msg or not msg.location:
        return
    loc = msg.location
    await asyncio.to_thread(cleanup_location_retention, habit_repo, user_id)
    await habit_location.handle_location(
        user_id,
        loc.latitude,
        loc.longitude,
        getattr(loc, "horizontal_accuracy", None),
    )


def _habit_threshold_from_text(text: str) -> int | None:
    low = text.lower()
    if "twice" in low or "two" in low:
        return 2
    m = re.search(r"\b(\d+)\b", low)
    return int(m.group(1)) if m else None


def _maybe_start_habit_request(user_id: str, text: str) -> bool:
    low = text.lower()
    if "fast food" in low and any(w in low for w in ("tell", "let me know", "track", "watch", "frequently")):
        threshold = _habit_threshold_from_text(text)
        if not threshold:
            pending_habit_watchers[user_id] = {"target_category": "fast_food"}
            _send_telegram(user_id, "What limit would you like to use? For example, more than two fast-food visits per week.")
            return True
        habit_watchers.create_habit_watcher(
            user_id, "weekly_visit_limit", target_category="fast_food",
            threshold_count=threshold, window_days=7,
        )
        _send_telegram(user_id, f"Got it. I'll let you know when you exceed {threshold} fast-food visits during a calendar week.")
        return True
    if "grocery" in low and "remind me" in low:
        reminder = text.split("remind me to", 1)[-1].strip() if "remind me to" in low else text
        habit_watchers.create_habit_watcher(
            user_id, "near_category_reminder", target_category="grocery_store",
            reminder_text=reminder,
        )
        _send_telegram(user_id, "Got it. I'll remind you when you are near a grocery store.")
        return True
    return False


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    if not _is_authorized(user_id):
        print(f"[auth] blocked message from user {user_id}")
        await update.message.reply_text("Sorry, this is a private bot.")
        return
    user_text = update.message.text

    if user_id in pending_forget:
        decision = _classify_confirmation(user_text)
        if decision == "send":
            target = pending_forget.pop(user_id)
            if target == "locations":
                await asyncio.to_thread(habit_repo.delete_locations, user_id)
                await update.message.reply_text("Deleted stored location data and turned location processing off.")
            else:
                await asyncio.to_thread(habit_repo.delete_habits, user_id)
                await update.message.reply_text("Deleted habit rules, suggestions, and habit nudge history.")
            return
        if decision == "cancel":
            pending_forget.pop(user_id)
            await update.message.reply_text("Cancelled. Nothing was deleted.")
            return
        await update.message.reply_text("Reply 'yes' to confirm deletion or 'no' to cancel.")
        return

    # Confirm-before-send: if a draft is pending, this message is the yes/no decision.
    if user_id in pending_emails:
        decision = _classify_confirmation(user_text)
        if decision == "send":
            draft = pending_emails.pop(user_id)
            result = _send_email_now(user_id, draft["to"], draft["subject"], draft["body"])
            await update.message.reply_text(result)
            return
        if decision == "cancel":
            pending_emails.pop(user_id)
            await update.message.reply_text("🚫 Draft discarded. Nothing was sent.")
            return
        await update.message.reply_text(
            "You have an email draft waiting. Reply 'yes' to send or 'no' to cancel."
        )
        return

    if user_id in pending_habit_watchers:
        threshold = _habit_threshold_from_text(user_text)
        if threshold:
            pending_habit_watchers.pop(user_id, None)
            await asyncio.to_thread(
                habit_watchers.create_habit_watcher,
                user_id, "weekly_visit_limit", "fast_food", None, threshold, 7, None,
            )
            await update.message.reply_text(
                f"Got it. I'll let you know when you exceed {threshold} fast-food visits during a calendar week."
            )
            return
        await update.message.reply_text("Please send a limit like 'more than twice per week'.")
        return

    if await asyncio.to_thread(_maybe_start_habit_request, user_id, user_text):
        return

    # Keyword automations: if the message matches a saved phrase, fire and stop.
    if await asyncio.to_thread(match_keyword_automations, user_id, user_text):
        return

    # Calendar fast-path: cheap add/edit/delete that skips the full tool loop.
    if await asyncio.to_thread(try_calendar_fastpath, user_id, user_text):
        return

    if user_id not in conversation_history:
        conversation_history[user_id] = deque(maxlen=10)

    # Blocking work runs in worker threads so the event loop stays free to
    # refresh the typing indicator and edit the status message live.
    status = StatusReporter(context, update.effective_chat.id)
    await status.start()
    try:
        long_term = await asyncio.to_thread(fetch_memories, user_id)
        system = BASE_SYSTEM_PROMPT + long_term

        model = await asyncio.to_thread(route_model, user_text)
        if model == "claude-sonnet-4-6":
            await status.update("💭 Using Sonnet...")

        def on_tool(tool_name: str) -> None:
            text = TOOL_STATUS.get(tool_name)
            if text:
                status.update_threadsafe(text)

        conversation_history[user_id].append({"role": "user", "content": user_text})
        reply = await asyncio.to_thread(
            call_claude, list(conversation_history[user_id]),
            system, user_id, model, on_tool,
        )
        conversation_history[user_id].append({"role": "assistant", "content": reply})

        await asyncio.to_thread(maybe_save_memory, user_id, user_text, reply)
    except Exception as e:
        await status.finish()
        print(f"[handler] error: {type(e).__name__}: {e}")
        await update.message.reply_text("Sorry — something went wrong. Please try again.")
        return

    await status.finish()
    await update.message.reply_text(reply)


# ---------------------------------------------------------------------------
# Automations ("recipes") — user-defined trigger -> action rules
#
# Design
#   A row in the `automations` table is: trigger (schedule | event | keyword) +
#   optional natural-language condition + action (one of our tools).
#   The engine runs IN-PROCESS inside the bot so it shares run_tool, the
#   pending_emails confirm flow, the Supabase client, and Claude.
#
#   • schedule  — checked every ~60s on the existing _scheduler loop via croniter
#                 (cron is interpreted in KST). last_run gates re-firing.
#   • event     — polled every ~15 min: new emails are diffed against a seen-id
#                 baseline file, separate from the digest's own state.
#   • keyword   — checked in handle_message before the normal LLM flow.
#
#   The optional condition is evaluated by a cheap Haiku yes/no against the
#   trigger's context (the event text / the incoming message). It fails safe:
#   if the check errors, the action is skipped.
#
#   Side-effectful actions keep the confirm-before-send rule: an unattended
#   send_email is staged into pending_emails and the user is pinged to reply
#   yes/no, reusing the exact same confirmation path as interactive emails.
# ---------------------------------------------------------------------------

# Tools an automation's action may invoke (send_message is automation-only).
ACTION_TOOLS = {
    "send_message", "save_reminder", "list_upcoming_events", "create_event",
    "list_recent_emails", "read_email", "send_email",
}

# "Action-tier" tools have outward/booking side-effects: they must always be
# confirmed before each run and are never executed silently by an automation.
ACTION_TIER_TOOLS = {"send_email", "create_event"}


def _tier(action: dict) -> str:
    return "action" if (action or {}).get("tool") in ACTION_TIER_TOOLS else "read_only"

EMAIL_EVENT_STATE = Path(__file__).with_name("automations_email_state.json")

SSL_CTX = ssl.create_default_context(cafile=certifi.where())


def _send_telegram(chat_id: str, text: str, buttons: list | None = None) -> None:
    """Send a message to a chat. `buttons` = [(label, callback_data), ...] adds
    a single-row inline keyboard (used for tap-to-approve and run-confirm)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return
    params = {"chat_id": chat_id, "text": text[:4000]}
    if buttons:
        params["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": l, "callback_data": d} for l, d in buttons]]}
        )
    data = urllib.parse.urlencode(params).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
            r.read()
    except Exception as e:
        print(f"[auto] telegram send failed: {e}")


# ---- parsing & summaries --------------------------------------------------

AUTOMATION_PARSE_SYSTEM = """You convert a user's plain-language automation rule into strict JSON.

Output ONLY a JSON object (no prose, no code fences) with these keys:
  description    : short restatement of the rule (the user's intent)
  trigger_type   : "schedule" | "event" | "keyword"
  trigger_config : object, depends on trigger_type:
       schedule -> {"cron": "<m h dom mon dow>"}  (5-field cron, interpreted in KST / Asia-Seoul)
       event    -> {"event": "email",
                    optional "from": "<sender substring>",
                    optional "subject_contains": "<substring>"}
       keyword  -> {"phrase": "<lowercase phrase to match in incoming messages>"}
  condition      : a natural-language condition string, or null if none
  action         : {"tool": "<tool name>", "input": { ... }}

Available action tools and their input fields:
  send_message         {"text": "<message to send the user>"}
  save_reminder        {"text": "...", "remind_at": "<ISO 8601>"}
  list_upcoming_events {"max_results": <int, optional>}
  create_event         {"title": "...", "start": "<ISO>", "end": "<ISO>"}
  list_recent_emails   {"count": <int, optional>}
  read_email           {"id": "<gmail id>"}
  send_email           {"to": "...", "subject": "...", "body": "..."}   (the user will confirm before it sends)

Rules:
- Map natural times to cron: "every morning at 8" -> "0 8 * * *", "every Monday 9am" -> "0 9 * * 1".
- Use an event trigger for "when I get an email".
- Use a keyword trigger for "when I say / text X".
- If the rule only notifies the user, use the send_message action.
- Put any "only if ..." part into condition, NOT into the trigger.
"""


def parse_automation(rule_text: str) -> dict:
    """One Claude call: plain-language rule -> validated structured automation."""
    resp = claude.messages.create(
        model="claude-sonnet-4-6", max_tokens=600,
        system=AUTOMATION_PARSE_SYSTEM,
        messages=[{"role": "user", "content": rule_text}],
    )
    raw = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    # Be forgiving if the model wraps it in ```...``` or adds stray text.
    if "{" in raw and "}" in raw:
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
    parsed = json.loads(raw)

    tt = parsed.get("trigger_type")
    if tt not in ("schedule", "event", "keyword"):
        raise ValueError(f"unknown trigger_type {tt!r}")
    action = parsed.get("action") or {}
    if action.get("tool") not in ACTION_TOOLS:
        raise ValueError(f"unknown action tool {action.get('tool')!r}")
    return parsed


def _trigger_summary(row: dict) -> str:
    tt = row["trigger_type"]
    cfg = row.get("trigger_config") or {}
    if tt == "schedule":
        return f"schedule (cron {cfg.get('cron') or cfg.get('time')}, KST)"
    if tt == "event":
        extra = ""
        if cfg.get("from"):
            extra += f" from~{cfg['from']}"
        if cfg.get("subject_contains"):
            extra += f" subject~{cfg['subject_contains']}"
        return f"on new {cfg.get('event')}{extra}"
    if tt == "keyword":
        return f"when a message contains '{cfg.get('phrase')}'"
    return tt


def _action_summary(action: dict) -> str:
    tool = (action or {}).get("tool", "?")
    inp = (action or {}).get("input") or {}
    if tool == "send_message":
        return f'message you: "{inp.get("text", "")[:60]}"'
    if tool == "send_email":
        return f"email {inp.get('to', '?')} (asks you to confirm)"
    if tool == "create_event":
        return f"add calendar event '{inp.get('title', '?')}' (asks you to confirm)"
    return f"run {tool}"


# ---- condition + execution ------------------------------------------------

def condition_met(condition: str, context: str) -> bool:
    """Cheap yes/no check. Fails safe (False) so side-effects never run on error."""
    prompt = (
        f"Condition: {condition}\n"
        f"Context: {context}\n"
        "Does the context satisfy the condition? Reply ONLY 'yes' or 'no'."
    )
    try:
        r = claude.messages.create(
            model="claude-haiku-4-5", max_tokens=3,
            messages=[{"role": "user", "content": prompt}],
        )
        return r.content[0].text.strip().lower().startswith("y")
    except Exception as e:
        print(f"[auto] condition check error: {e}")
        return False


def execute_automation(auto: dict, context_text: str = "") -> bool:
    """Run one automation's action. Returns True if it acted, False if skipped."""
    user_id = auto["user_id"]
    cond = auto.get("condition")
    if cond and not condition_met(cond, context_text or auto.get("description", "")):
        print(f"[auto] {str(auto.get('id'))[:8]} skipped — condition not met")
        return False

    action = auto.get("action") or {}
    tool = action.get("tool")
    inp = action.get("input") or {}
    if not tool:
        return False

    if tool == "send_email":
        # Never send unattended — stage the draft and ask, reusing pending_emails.
        pending_emails[user_id] = {
            "to": inp.get("to", ""), "subject": inp.get("subject", ""),
            "body": inp.get("body", ""),
        }
        _send_telegram(
            user_id,
            f"🤖 Automation: {auto.get('description', '')}\n"
            "I want to send this email — reply 'yes' to send or 'no' to cancel:\n"
            f"To: {inp.get('to')}\nSubject: {inp.get('subject')}\n\n{inp.get('body')}"
        )
        return True

    if tool in ACTION_TIER_TOOLS:
        # Other action-tier tools (e.g. create_event): confirm before every run
        # with a tap, so an automation never performs them silently.
        act_id = uuid.uuid4().hex
        pending_actions[act_id] = {
            "user_id": user_id, "tool": tool, "input": inp,
            "description": auto.get("description", ""),
        }
        _send_telegram(
            user_id,
            f"🤖 Automation '{auto.get('description', '')}' wants to {_action_summary(action)}.",
            buttons=[("✅ Run it", f"act:run:{act_id}"), ("❌ Skip", f"act:skip:{act_id}")],
        )
        return True

    try:
        result = run_tool(tool, inp, user_id)
    except Exception as e:
        result = f"(automation action failed: {e})"
    _send_telegram(user_id, f"🤖 {auto.get('description', '')}\n{result}".strip())
    return True


def _mark_run(auto_id) -> None:
    try:
        supabase.table("automations").update(
            {"last_run": datetime.now(timezone.utc).isoformat()}
        ).eq("id", auto_id).execute()
    except Exception as e:
        print(f"[auto] mark_run failed for {auto_id}: {e}")


def _enabled(trigger_type: str) -> list:
    try:
        return (
            supabase.table("automations").select("*")
            .eq("trigger_type", trigger_type).eq("enabled", True)
            .execute().data or []
        )
    except Exception as e:
        print(f"[auto] fetch {trigger_type} failed: {e}")
        return []


# ---- CRUD (called from run_tool) ------------------------------------------

def create_automation(user_id: str, rule_text: str) -> str:
    try:
        parsed = parse_automation(rule_text)
    except Exception as e:
        return f"Couldn't understand that automation: {e}"
    row = {
        "user_id": user_id,
        "description": parsed.get("description") or rule_text,
        "trigger_type": parsed["trigger_type"],
        "trigger_config": parsed.get("trigger_config") or {},
        "condition": parsed.get("condition"),
        "action": parsed["action"],
        "enabled": True,
    }
    try:
        res = supabase.table("automations").insert(row).execute()
        new_id = str((res.data or [{}])[0].get("id", "?"))
    except Exception as e:
        return f"Failed to save automation: {e}"
    return (
        f"✅ Automation saved (id {new_id[:8]}).\n"
        f"What it does: {row['description']}\n"
        f"Trigger: {_trigger_summary(row)}\n"
        f"Action: {_action_summary(row['action'])}"
        + (f"\nOnly when: {row['condition']}" if row.get("condition") else "")
    )


def list_automations(user_id: str) -> str:
    try:
        rows = (
            supabase.table("automations").select("*")
            .eq("user_id", user_id).order("created_at").execute().data or []
        )
    except Exception as e:
        return f"Couldn't list automations: {e}"
    if not rows:
        return "You have no automations yet. Say: create an automation: <your rule>"
    lines = []
    for r in rows:
        dot = "🟢" if r.get("enabled") else "⚪️"
        lines.append(
            f"{dot} [{str(r['id'])[:8]}] {r.get('description', '')}\n"
            f"     {_trigger_summary(r)} → {_action_summary(r.get('action') or {})}"
        )
    return "\n".join(lines)


def _resolve_automation(user_id: str, ref: str):
    """Return (row, error). Match by id / id-prefix, else by unique description."""
    try:
        rows = (
            supabase.table("automations").select("*")
            .eq("user_id", user_id).execute().data or []
        )
    except Exception as e:
        return None, f"lookup failed: {e}"
    ref_l = ref.strip().lower()
    by_id = [r for r in rows if str(r["id"]).lower().startswith(ref_l)]
    if len(by_id) == 1:
        return by_id[0], None
    by_desc = [r for r in rows if ref_l in (r.get("description") or "").lower()]
    if len(by_desc) == 1:
        return by_desc[0], None
    if not by_id and not by_desc:
        return None, "No automation matches that."
    return None, "That matches more than one automation — use the id shown in the list."


def set_automation_enabled(user_id: str, ref: str, enabled: bool) -> str:
    row, err = _resolve_automation(user_id, ref)
    if err:
        return err
    try:
        supabase.table("automations").update({"enabled": enabled}).eq("id", row["id"]).execute()
    except Exception as e:
        return f"Failed to update: {e}"
    state = "enabled" if enabled else "disabled"
    return f"{'🟢' if enabled else '⚪️'} {state}: {row.get('description', '')}"


def delete_automation(user_id: str, ref: str) -> str:
    row, err = _resolve_automation(user_id, ref)
    if err:
        return err
    try:
        supabase.table("automations").delete().eq("id", row["id"]).execute()
    except Exception as e:
        return f"Failed to delete: {e}"
    return f"🗑️ Deleted: {row.get('description', '')}"


# ---- trigger engine -------------------------------------------------------

def _parse_ts(s) -> datetime:
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _time_to_cron(t):
    if not t:
        return None
    try:
        hh, mm = str(t).split(":")
        return f"{int(mm)} {int(hh)} * * *"
    except Exception:
        return None


def run_due_schedule_automations() -> None:
    """Fire schedule automations whose next cron occurrence has passed. Blocking."""
    if croniter is None:
        return
    now_kst = datetime.now(KST).replace(tzinfo=None)
    for a in _enabled("schedule"):
        cfg = a.get("trigger_config") or {}
        cron = cfg.get("cron") or _time_to_cron(cfg.get("time"))
        if not cron:
            continue
        last = a.get("last_run")
        base = (_parse_ts(last).astimezone(KST).replace(tzinfo=None)
                if last else now_kst - timedelta(minutes=1))
        try:
            nxt = croniter(cron, base).get_next(datetime)
        except Exception as e:
            print(f"[auto] bad cron {cron!r} for {str(a['id'])[:8]}: {e}")
            continue
        if nxt <= now_kst:
            _mark_run(a["id"])            # mark first so it can't re-fire next tick
            execute_automation(a, context_text=f"Scheduled run at {now_kst.isoformat()}")


def poll_event_automations() -> None:
    """Detect new emails and fire matching automations. Blocking."""
    autos = _enabled("event")
    if not autos:
        return
    email_autos = [a for a in autos if (a.get("trigger_config") or {}).get("event") == "email"]
    if email_autos:
        _poll_email_events(email_autos)


def _email_meta(user_id: str, count: int = 10):
    service = _gmail_service(user_id)
    if not service:
        return None
    try:
        resp = service.users().messages().list(
            userId="me", maxResults=count, labelIds=["INBOX"]
        ).execute()
        out = []
        for msg in resp.get("messages", []):
            full = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()
            h = {x["name"]: x["value"] for x in full["payload"]["headers"]}
            out.append({"id": msg["id"], "subject": h.get("Subject", "(no subject)"),
                        "sender": h.get("From", "?")})
        return out
    except Exception as e:
        print(f"[auto] email meta failed: {e}")
        return None


def _poll_email_events(autos: list) -> None:
    baseline = {}
    if EMAIL_EVENT_STATE.exists():
        try:
            baseline = json.loads(EMAIL_EVENT_STATE.read_text(encoding="utf-8"))
        except Exception:
            baseline = {}
    for uid in {a["user_id"] for a in autos}:
        metas = _email_meta(uid, 10)
        if metas is None:
            continue
        seen = baseline.get(uid)
        baseline[uid] = [m["id"] for m in metas]
        if not seen:
            continue                      # first run: baseline only
        seen_set = set(seen)
        for m in reversed(metas):         # oldest new email first
            if m["id"] in seen_set:
                continue
            ctx = f"New email from {m['sender']} — subject: {m['subject']}"
            for a in autos:
                if a["user_id"] != uid:
                    continue
                cfg = a.get("trigger_config") or {}
                if cfg.get("from") and cfg["from"].lower() not in m["sender"].lower():
                    continue
                if cfg.get("subject_contains") and \
                        cfg["subject_contains"].lower() not in m["subject"].lower():
                    continue
                if execute_automation(a, context_text=ctx):
                    _mark_run(a["id"])
    try:
        EMAIL_EVENT_STATE.write_text(
            json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"[auto] email baseline save failed: {e}")


def match_keyword_automations(user_id: str, text: str) -> bool:
    """Fire keyword automations whose phrase appears in the message. Blocking."""
    try:
        rows = (
            supabase.table("automations").select("*")
            .eq("trigger_type", "keyword").eq("enabled", True).eq("user_id", user_id)
            .execute().data or []
        )
    except Exception as e:
        print(f"[auto] keyword fetch failed: {e}")
        return False
    low = text.lower()
    fired = False
    for a in rows:
        phrase = ((a.get("trigger_config") or {}).get("phrase") or "").lower().strip()
        if phrase and phrase in low:
            if execute_automation(a, context_text=text):
                _mark_run(a["id"])
                fired = True
    return fired


# ---------------------------------------------------------------------------
# Auto-suggestion loop — the assistant proposes automations on its own
#
# Flow: detect -> propose -> approve
#   LOG     : call_claude records each USER-initiated tool call to activity_log
#             (automation-driven run_tool calls are NOT logged, so the log is a
#             clean picture of what the user actually does).
#   DETECT  : every ~3 days run_suggestion_cycle() asks Claude to review recent
#             activity + memory and return 0-2 concrete proposals (Phase 1
#             format). It is given a denylist so it never repeats a pattern the
#             user already has, already saw, or dismissed.
#   PROPOSE : each proposal is saved (automation_suggestions, status 'pending')
#             and sent with inline buttons [Yes, set it up]/[No thanks]. The
#             button only carries the row id (Telegram caps callback_data at 64B).
#   APPROVE : Yes  -> build a Phase 1 automation from the row, insert + enable.
#             No   -> mark 'dismissed'; its pattern_key stays on the denylist,
#                     which is how a dismissed pattern is "remembered".
#
# Risk tiers: read-only automations are suggested freely; action-tier ones
# (send_email / create_event) still need the Yes tap to be created AND keep a
# per-run confirm (see execute_automation) — they never auto-run silently.
#
# Anti-spam: max 2 per cycle; a cycle is skipped entirely while any proposal is
# still unanswered; dismissed patterns are never re-suggested; the user can turn
# suggestions off (set_suggestions / "stop suggesting automations").
# ---------------------------------------------------------------------------

# Management tools are noise for pattern detection — don't log them as activity.
SKIP_LOG = {
    "create_automation", "list_automations", "set_automation", "delete_automation",
    "set_suggestions", "send_message", "create_habit_watcher", "list_habit_watchers",
    "delete_habit_watcher", "set_habit_suggestions_enabled", "get_recent_place_visits",
}

SUGGEST_STATE = Path(__file__).with_name("suggestion_state.json")
SUGGEST_EVERY = 3 * 86400          # propose at most once every ~3 days
MAX_PER_CYCLE = 2


def _log_activity(user_id: str, tool: str, tool_input) -> None:
    if tool in SKIP_LOG:
        return
    try:
        detail = ""
        if isinstance(tool_input, dict) and tool_input:
            detail = "; ".join(f"{k}={str(v)[:40]}" for k, v in list(tool_input.items())[:3])
        supabase.table("activity_log").insert(
            {"user_id": user_id, "tool": tool, "detail": detail[:300]}
        ).execute()
    except Exception as e:
        print(f"[activity] log failed: {e}")


def _recent_activity(user_id: str, days: int = 14, limit: int = 200) -> str:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        rows = (
            supabase.table("activity_log").select("*")
            .eq("user_id", user_id).gte("created_at", since)
            .order("created_at").execute().data or []
        )
    except Exception as e:
        print(f"[suggest] activity fetch failed: {e}")
        return ""
    lines = []
    for r in rows[-limit:]:
        ts = _parse_ts(r["created_at"]).astimezone(KST).strftime("%a %m-%d %H:%M")
        lines.append(f"{ts} KST  {r['tool']}  {r.get('detail', '')}".rstrip())
    return "\n".join(lines)


DETECT_SYSTEM = """You review a user's recent assistant activity and propose at most 2 NEW automations
that would genuinely save them effort, based on CLEAR, REPEATED patterns.

Be conservative. If there is no obvious recurring pattern, return an empty array [].
Never propose something similar to the user's existing automations or to anything in the
do-not-suggest list.

Output ONLY a JSON array (0 to 2 objects). Each object:
  pattern_key    : short stable kebab-case signature of the pattern (e.g. "morning-schedule-check")
  rationale      : ONE friendly sentence stating the observation and the offer, e.g.
                   "I noticed you check your schedule most mornings — want me to send it to you automatically at 8am?"
  description    : short restatement of what the automation does
  trigger_type   : "schedule" | "event" | "keyword"
  trigger_config : schedule -> {"cron": "<m h dom mon dow>"} (KST);
                   event -> {"event": "email", optional "from", "subject_contains"};
                   keyword -> {"phrase": "<lowercase phrase>"}
  condition      : natural-language condition or null
  action         : {"tool": "<name>", "input": {...}}

Action tools: send_message {"text"}, save_reminder {"text","remind_at"},
list_upcoming_events {}, list_recent_emails {},
create_event {"title","start","end"}, send_email {"to","subject","body"}.

Strongly prefer read-only actions (send_message / surfacing info / reminders). Only propose
create_event or send_email if the repeated pattern is very clear; the user will still confirm
before those ever run."""


def detect_automation_proposals(user_id: str) -> list[dict]:
    activity = _recent_activity(user_id)
    if not activity.strip():
        return []
    memories = fetch_memories(user_id)
    try:
        existing = [
            r.get("description", "") for r in
            (supabase.table("automations").select("description")
             .eq("user_id", user_id).execute().data or [])
        ]
    except Exception:
        existing = []
    try:
        seen = (supabase.table("automation_suggestions")
                .select("pattern_key,description").eq("user_id", user_id).execute().data or [])
    except Exception:
        seen = []
    denylist_keys = {(s.get("pattern_key") or "").lower() for s in seen}

    context = (
        "RECENT ACTIVITY (most recent last):\n" + activity + "\n\n"
        + (memories or "") + "\n\n"
        + "EXISTING AUTOMATIONS (do not duplicate):\n"
        + ("\n".join(f"- {d}" for d in existing) if existing else "  none") + "\n\n"
        + "DO-NOT-SUGGEST patterns (already proposed or dismissed):\n"
        + ("\n".join(f"- {s.get('pattern_key')}: {s.get('description')}" for s in seen) if seen else "  none")
    )
    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-6", max_tokens=800,
            system=DETECT_SYSTEM, messages=[{"role": "user", "content": context}],
        )
        raw = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        if "[" in raw and "]" in raw:
            raw = raw[raw.find("["): raw.rfind("]") + 1]
        items = json.loads(raw)
    except Exception as e:
        print(f"[suggest] detect failed: {e}")
        return []
    if not isinstance(items, list):
        return []

    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if it.get("trigger_type") not in ("schedule", "event", "keyword"):
            continue
        if (it.get("action") or {}).get("tool") not in ACTION_TOOLS:
            continue
        key = (it.get("pattern_key") or "").lower()
        if not key or key in denylist_keys:
            continue
        out.append(it)
        if len(out) >= MAX_PER_CYCLE:
            break
    return out


def suggestions_enabled(user_id: str) -> bool:
    try:
        rows = (supabase.table("suggestion_prefs").select("enabled")
                .eq("user_id", user_id).execute().data or [])
        return bool(rows[0]["enabled"]) if rows else True
    except Exception:
        return True


def set_suggestions(user_id: str, enabled: bool) -> str:
    try:
        supabase.table("suggestion_prefs").upsert(
            {"user_id": user_id, "enabled": enabled}
        ).execute()
    except Exception as e:
        return f"Couldn't change that setting: {e}"
    return ("🔔 I'll suggest automations when I spot useful patterns."
            if enabled else
            "🔕 Okay — I won't suggest automations anymore. Say 'suggest automations again' to re-enable.")


def run_suggestion_cycle() -> None:
    """Detect patterns and send tap-to-approve proposals. Blocking (runs in a thread)."""
    try:
        recent = (supabase.table("activity_log").select("user_id").execute().data or [])
        users = {r["user_id"] for r in recent}
    except Exception as e:
        print(f"[suggest] user scan failed: {e}")
        users = set()
    if not users:
        users = set(ALLOWED_USER_IDS)

    for uid in users:
        if not suggestions_enabled(uid):
            continue
        try:                                  # anti-spam: skip if anything's unanswered
            pending = (supabase.table("automation_suggestions").select("id")
                       .eq("user_id", uid).eq("status", "pending").execute().data or [])
        except Exception:
            pending = []
        if pending:
            continue
        for p in detect_automation_proposals(uid):
            row = {
                "user_id": uid,
                "pattern_key": p.get("pattern_key"),
                "rationale": p.get("rationale") or "",
                "description": p.get("description") or "",
                "trigger_type": p["trigger_type"],
                "trigger_config": p.get("trigger_config") or {},
                "condition": p.get("condition"),
                "action": p["action"],
                "tier": _tier(p.get("action")),
                "status": "pending",
            }
            try:
                res = supabase.table("automation_suggestions").insert(row).execute()
                sid = str((res.data or [{}])[0].get("id"))
            except Exception as e:
                print(f"[suggest] save failed: {e}")
                continue
            msg = row["rationale"] or f"Want me to set up: {row['description']}?"
            if row["tier"] == "action":
                msg += "\n(I'll always ask before it actually runs.)"
            _send_telegram(uid, msg, buttons=[
                ("✅ Yes, set it up", f"sug:yes:{sid}"),
                ("❌ No thanks", f"sug:no:{sid}"),
            ])


def approve_suggestion(user_id: str, sid: str) -> str:
    try:
        rows = (supabase.table("automation_suggestions").select("*")
                .eq("id", sid).eq("user_id", user_id).execute().data or [])
    except Exception as e:
        return f"Couldn't load that suggestion: {e}"
    if not rows:
        return "That suggestion has expired."
    s = rows[0]
    if s.get("status") != "pending":
        return "That suggestion was already handled."
    auto = {
        "user_id": user_id,
        "description": s.get("description") or "",
        "trigger_type": s.get("trigger_type"),
        "trigger_config": s.get("trigger_config") or {},
        "condition": s.get("condition"),
        "action": s.get("action") or {},
        "enabled": True,
    }
    try:
        supabase.table("automations").insert(auto).execute()
        supabase.table("automation_suggestions").update({"status": "approved"}).eq("id", sid).execute()
    except Exception as e:
        return f"Failed to set it up: {e}"
    note = "\n(I'll always ask before it runs.)" if _tier(auto["action"]) == "action" else ""
    return (f"✅ Set up: {auto['description']}\n"
            f"Trigger: {_trigger_summary(auto)} → {_action_summary(auto['action'])}{note}")


def dismiss_suggestion(user_id: str, sid: str) -> str:
    try:
        supabase.table("automation_suggestions").update({"status": "dismissed"}) \
            .eq("id", sid).eq("user_id", user_id).execute()
    except Exception as e:
        return f"Couldn't dismiss that: {e}"
    return "👍 Got it — I won't suggest that again."


def run_pending_action(user_id: str, act_id: str) -> str:
    pa = pending_actions.pop(act_id, None)
    if not pa or pa.get("user_id") != user_id:
        return "That action has expired."
    try:
        result = run_tool(pa["tool"], pa.get("input") or {}, user_id)
    except Exception as e:
        return f"Action failed: {e}"
    return f"✅ {result}"


def _suggest_due() -> bool:
    try:
        if SUGGEST_STATE.exists():
            last = _parse_ts(json.loads(SUGGEST_STATE.read_text())["last_detect"])
            return (datetime.now(timezone.utc) - last).total_seconds() >= SUGGEST_EVERY
    except Exception:
        pass
    return True


def _mark_suggest_run() -> None:
    try:
        SUGGEST_STATE.write_text(json.dumps({"last_detect": datetime.now(timezone.utc).isoformat()}))
    except Exception as e:
        print(f"[suggest] state write failed: {e}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Routes inline-keyboard taps: suggestion approve/dismiss and action run/skip."""
    query = update.callback_query
    user_id = str(update.effective_user.id)
    await query.answer()
    if not _is_authorized(user_id):
        await query.edit_message_text("Sorry, this is a private bot.")
        return
    try:
        kind, action, ref = (query.data or "").split(":", 2)
    except ValueError:
        return
    if kind == "sug" and action == "yes":
        msg = await asyncio.to_thread(approve_suggestion, user_id, ref)
    elif kind == "sug" and action == "no":
        msg = await asyncio.to_thread(dismiss_suggestion, user_id, ref)
    elif kind == "act" and action == "run":
        msg = await asyncio.to_thread(run_pending_action, user_id, ref)
    elif kind == "act" and action == "skip":
        pending_actions.pop(ref, None)
        msg = "👍 Skipped."
    elif kind == "cal" and action == "yes":
        msg = await asyncio.to_thread(commit_calendar_op, user_id, ref)
    elif kind == "cal" and action == "no":
        pending_calendar.pop(ref, None)
        msg = "🚫 Cancelled."
    elif kind == "habit" and action == "yes":
        msg = await asyncio.to_thread(habit_policies.confirm_habit_suggestion, user_id, ref, True)
    elif kind == "habit" and action == "no":
        msg = await asyncio.to_thread(habit_policies.confirm_habit_suggestion, user_id, ref, False)
    elif kind == "habit" and action == "mute":
        msg = await asyncio.to_thread(habit_policies.mute_suggestion_category, user_id, ref)
    else:
        return
    try:
        await query.edit_message_text(msg)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Background scheduler — one Railway service runs the bot + digest + reminders
# ---------------------------------------------------------------------------

DIGEST_SCRIPT = Path(__file__).with_name("daily_digest.py")
REMINDER_SCRIPT = Path(__file__).with_name("reminder_check.py")
ENABLE_SCHEDULER = os.environ.get("ENABLE_SCHEDULER", "1") != "0"


async def _run_script(*args: str) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        if out:
            print(f"[sched] {Path(args[0]).name}: {out.decode(errors='replace').strip()[:300]}")
    except Exception as e:
        print(f"[sched] failed {args}: {e}")


async def _scheduler() -> None:
    last_reminder = 0.0
    fired: dict[str, set] = {}
    while True:
        try:
            now = datetime.now(KST)
            t = asyncio.get_event_loop().time()
            # Schedule automations: checked every tick (~60s). Blocking work
            # (LLM/condition/tool/send) runs off the event loop in a thread.
            await asyncio.to_thread(run_due_schedule_automations)
            if t - last_reminder >= 900:                  # reminders every 15 min
                last_reminder = t
                await _run_script(str(REMINDER_SCRIPT))
                await asyncio.to_thread(poll_event_automations)   # new emails
                if _suggest_due():                                # propose automations ~every 3 days
                    _mark_suggest_run()
                    await asyncio.to_thread(run_suggestion_cycle)
            done = fired.setdefault(now.date().isoformat(), set())
            if now.hour == 9 and "m" not in done:         # morning digest, 09:00 KST
                done.add("m")
                await _run_script(str(DIGEST_SCRIPT))
            if now.hour == 21 and "e" not in done:        # evening digest, 21:00 KST
                done.add("e")
                await _run_script(str(DIGEST_SCRIPT), "--evening")
        except Exception as e:
            print(f"[sched] loop error: {e}")
        await asyncio.sleep(60)


async def _post_init(application) -> None:
    if ENABLE_SCHEDULER:
        application.create_task(_scheduler())
        print("[sched] background scheduler started (digest + reminders + automations)")


def _start_oauth_server() -> None:
    """
    Serve the Google OAuth callback in-process so connecting works on Railway.

    The bot itself uses long-polling (no inbound port), so we use Railway's
    $PORT for the callback web server. Reuses oauth_server.app (the /oauth/callback
    endpoint that exchanges the code and upserts tokens) rather than duplicating it.
    Runs in a daemon thread with its own event loop, separate from run_polling.
    """
    import threading
    import uvicorn
    import oauth_server

    port = int(os.environ.get("PORT", "8080"))

    # uvicorn.run() installs OS signal handlers, which only works in the main
    # thread; run_polling() owns that thread, so we drive the Server manually
    # and skip signal handling in this worker thread.
    config = uvicorn.Config(oauth_server.app, host="0.0.0.0", port=port,
                            log_level="warning")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None

    threading.Thread(target=server.run, daemon=True).start()
    print(f"[oauth] callback server listening on 0.0.0.0:{port}")


def main() -> None:
    _start_oauth_server()
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    if not ALLOWED_USER_IDS:
        print("[auth] WARNING: no TELEGRAM_ALLOWED_IDS / TELEGRAM_CHAT_ID set — "
              "bot will deny everyone. Set your Telegram id to use it.")
    else:
        print(f"[auth] bot restricted to user id(s): {', '.join(sorted(ALLOWED_USER_IDS))}")
    app = ApplicationBuilder().token(token).post_init(_post_init).build()
    app.add_handler(CommandHandler("geoloc", handle_geoloc))
    app.add_handler(CommandHandler("location_status", handle_location_status))
    app.add_handler(CommandHandler("location_off", handle_location_off))
    app.add_handler(CommandHandler("nudges_on", handle_nudges_on))
    app.add_handler(CommandHandler("nudges_off", handle_nudges_off))
    app.add_handler(CommandHandler("nudges_status", handle_nudges_status))
    app.add_handler(CommandHandler("visits", handle_visits))
    app.add_handler(CommandHandler("habit_watchers", handle_habit_watchers))
    app.add_handler(CommandHandler("habit_suggestions_on", handle_habit_suggestions_on))
    app.add_handler(CommandHandler("habit_suggestions_off", handle_habit_suggestions_off))
    app.add_handler(CommandHandler("delete_watcher", handle_delete_watcher))
    app.add_handler(CommandHandler("forget_locations", handle_forget_locations))
    app.add_handler(CommandHandler("forget_habits", handle_forget_habits))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location_update))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.LOCATION, handle_location_update))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot running — press Ctrl+C to stop")
    app.run_polling()


if __name__ == "__main__":
    main()
