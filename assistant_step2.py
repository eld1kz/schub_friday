import os
from collections import deque
from dotenv import load_dotenv
import anthropic
from supabase import create_client
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# --- Clients (created once at startup, reused for every message) ---
# SUPABASE_KEY must be the service_role key — it runs server-side and bypasses RLS.
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

# --- Short-term memory ---
# dict mapping user_id → deque of the last 10 messages.
# deque(maxlen=10) automatically drops the oldest entry when it's full.
# Lives in RAM only — resets when the bot restarts.
conversation_history: dict[str, deque] = {}

BASE_SYSTEM_PROMPT = (
    "You are a concise, friendly personal assistant. "
    "Keep replies short and direct. No unnecessary filler."
)


# ---------------------------------------------------------------------------
# Long-term memory helpers
# ---------------------------------------------------------------------------

def fetch_memories(user_id: str) -> str:
    """Return saved facts about this user as a formatted string, or ''."""
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
        print(f"[memory] fetch error (no long-term context this turn): {type(e).__name__}: {e}")
        return ""


def maybe_save_memory(user_id: str, user_text: str, assistant_reply: str) -> None:
    """
    Ask Claude whether the exchange contained a fact worth remembering.
    If yes, save it to Supabase. If Claude says NONE, do nothing.
    This is a cheap call: small prompt, max_tokens=80.
    """
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
# Message handler
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    user_text = update.message.text

    # 1. Initialise short-term history for first message from this user.
    if user_id not in conversation_history:
        conversation_history[user_id] = deque(maxlen=10)

    # 2. Fetch long-term memories and attach them to the system prompt.
    #    Claude sees this on every call, so it always "knows" the user.
    long_term = fetch_memories(user_id)
    system = BASE_SYSTEM_PROMPT + long_term

    # 3. Append the new user message to short-term history.
    conversation_history[user_id].append({"role": "user", "content": user_text})

    # 4. Send the full conversation history to Claude.
    #    list(deque) gives the messages in chronological order.
    response = claude.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1024,
        system=system,
        messages=list(conversation_history[user_id]),
    )
    reply = response.content[0].text

    # 5. Save Claude's reply to short-term history so the next call has context.
    conversation_history[user_id].append({"role": "assistant", "content": reply})

    # 6. Check whether this exchange contains a fact worth remembering.
    #    Runs synchronously here — fine for a personal bot; for high traffic
    #    you'd push this to a background task so it doesn't block the reply.
    maybe_save_memory(user_id, user_text, reply)

    await update.message.reply_text(reply)


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = ApplicationBuilder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot running — press Ctrl+C to stop")
    app.run_polling()


if __name__ == "__main__":
    main()
