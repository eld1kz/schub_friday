import os
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

import asyncio
import base64
import subprocess
import sys
from collections import deque
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from dotenv import load_dotenv
import anthropic
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from supabase import create_client
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

conversation_history: dict[str, deque] = {}
# Email drafts awaiting the user's yes/no confirmation before send_email sends.
pending_emails: dict[str, dict] = {}

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

REDIRECT_URI = "http://localhost:8080/oauth/callback"
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

BASE_SYSTEM_PROMPT = (
    "You are a concise, friendly personal assistant. "
    "Keep replies short and direct. No unnecessary filler. "
    "You have tools to check the time, save reminders, search the web, "
    "manage the user's Google Calendar, read and send Gmail, and check the "
    "user's university grades and assignments. "
    "When the user asks to send an email, call send_email to prepare a draft. "
    "It does NOT send immediately — show the user the drafted email and tell "
    "them to reply 'yes' to send or 'no' to cancel."
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
        "name": "check_grades",
        "description": "Checks the user's Korea University (LMS) grades and assignments "
                       "via the Canvas API: per-course current/final score plus each "
                       "assignment's score, submission status, and due date.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "check_assignments",
        "description": "Lists the user's university assignments via the Canvas API: which are "
                       "not submitted (with due dates, flagging overdue) and which are "
                       "submitted awaiting a grade. Use for 'what's due' / 'what do I owe'.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
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
        return "Google Calendar not connected. Send /connect to link your account."
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
        return "Google Calendar not connected. Send /connect to link your account."
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
        return "Gmail not connected. Send /connect to link your account."
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
        return "Gmail not connected. Send /connect to link your account."
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
        return "Gmail not connected. Send /connect to link your account."
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
# University grades (Canvas API, run as a subprocess)
# ---------------------------------------------------------------------------

GRADES_SCRIPT = Path(__file__).with_name("uni_api_grades.py")


def _run_uni_script(*args: str) -> str:
    """Run the Canvas API reader in a subprocess and return its stdout report."""
    try:
        proc = subprocess.run(
            [sys.executable, str(GRADES_SCRIPT), *args],
            capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        return "University check timed out. Try again in a moment."
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip()
        if "uni_login_test" in msg or "Session expired" in msg or "401" in msg:
            return ("University session expired. On the computer running the bot, run "
                    "`python uni_login_test.py` to log in again, then ask me to re-check.")
        return f"Could not read university data: {msg[:300]}"
    return proc.stdout.strip() or "No data found."


def check_university_grades() -> str:
    return _run_uni_script()


def check_university_assignments() -> str:
    return _run_uni_script("--assignments")


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

    if name == "check_grades":
        return check_university_grades()

    if name == "check_assignments":
        return check_university_assignments()

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
    "check_grades": "🎓 Checking your grades...",
    "check_assignments": "📝 Checking your assignments...",
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
# Telegram handlers
# ---------------------------------------------------------------------------

async def handle_connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /connect — generates the Google OAuth URL and sends it to the user.

    The `state` parameter carries the Telegram user_id through Google's
    redirect back to our server, so the callback knows whose tokens to save.

    access_type="offline" → Google returns a refresh_token (needed for long-term access).
    prompt="consent"      → forces the consent screen every time, guaranteeing a
                            fresh refresh_token even if the user connected before.
    """
    user_id = str(update.effective_user.id)
    if not _is_authorized(user_id):
        await update.message.reply_text("Sorry, this is a private bot.")
        return
    flow = Flow.from_client_config(
        GOOGLE_CLIENT_CONFIG, scopes=SCOPES, redirect_uri=REDIRECT_URI,
        autogenerate_code_verifier=False,   # disable PKCE: we have a client_secret, and the
                                            # token exchange happens in a separate process
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        state=user_id,
        prompt="consent",
    )
    await update.message.reply_text(
        "Open this link in a browser on the same computer as the bot, "
        "then approve access:\n\n" + auth_url
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    if not _is_authorized(user_id):
        print(f"[auth] blocked message from user {user_id}")
        await update.message.reply_text("Sorry, this is a private bot.")
        return
    user_text = update.message.text

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


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    if not ALLOWED_USER_IDS:
        print("[auth] WARNING: no TELEGRAM_ALLOWED_IDS / TELEGRAM_CHAT_ID set — "
              "bot will deny everyone. Set your Telegram id to use it.")
    else:
        print(f"[auth] bot restricted to user id(s): {', '.join(sorted(ALLOWED_USER_IDS))}")
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("connect", handle_connect))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot running — press Ctrl+C to stop")
    app.run_polling()


if __name__ == "__main__":
    main()
