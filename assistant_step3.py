import os
from collections import deque
from datetime import datetime
from dotenv import load_dotenv
import anthropic
from supabase import create_client
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

conversation_history: dict[str, deque] = {}

BASE_SYSTEM_PROMPT = (
    "You are a concise, friendly personal assistant. "
    "Keep replies short and direct. No unnecessary filler. "
    "You have tools to check the time, save reminders, and search the web."
)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------
# Each dict here is sent to Claude as a tool it can choose to call.
# Claude decides whether and when to call them — you just define what's available.

TOOLS = [
    # web_search is a built-in server-side tool.
    # Anthropic's servers run the actual search; no code needed on our side.
    {
        "type": "web_search_20250305",
        "name": "web_search",
    },
    {
        "name": "get_current_datetime",
        "description": "Returns the current local date and time.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "save_reminder",
        "description": (
            "Saves a reminder for the user. Call this when the user asks to be "
            "reminded about something at a specific time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The reminder text",
                },
                "remind_at": {
                    "type": "string",
                    "description": "ISO 8601 datetime string (e.g. 2026-06-05T09:00:00)",
                },
            },
            "required": ["text", "remind_at"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------
# This is the client-side half of tool use: you receive the tool name and
# arguments from Claude, run the actual logic, and return a result string.

def run_tool(name: str, tool_input: dict, user_id: str) -> str:
    if name == "get_current_datetime":
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if name == "save_reminder":
        try:
            supabase.table("reminders").insert({
                "user_id": user_id,
                "text": tool_input["text"],
                "remind_at": tool_input["remind_at"],
            }).execute()
            return f"Saved: '{tool_input['text']}' at {tool_input['remind_at']}"
        except Exception as e:
            return f"Failed to save reminder: {e}"

    return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# Tool-use loop
# ---------------------------------------------------------------------------

def call_claude(messages: list, system: str, user_id: str) -> str:
    """
    Call Claude, handle any tool calls, and return the final text reply.

    The loop runs until stop_reason == "end_turn":

      1. Call Claude with the current messages.
      2. "tool_use" → Claude wants to call one or more tools.
           a. Append Claude's response (which contains the tool_use blocks)
              as an assistant turn — this is required by the API so the next
              call has context on what Claude was doing.
           b. Run each requested tool and collect the results.
           c. Append results as a user turn with role "tool_result".
           d. Loop back to step 1.
      3. "end_turn" → Claude is done. Extract and return the text reply.

    We receive a copy of messages so intermediate tool_use / tool_result
    turns stay local to this function and don't pollute conversation_history.
    """
    while True:
        response = claude.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason in ("end_turn", "max_tokens"):
            return "".join(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason == "tool_use":
            # a. Append Claude's full response (may include a text preamble
            #    plus the tool_use blocks) as an assistant turn.
            messages.append({"role": "assistant", "content": response.content})

            # b & c. Run each tool and build the tool_result list.
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result = run_tool(block.name, block.input, user_id)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,   # must match the block's id
                    "content": result,
                })

            # d. Feed the results back and loop.
            messages.append({"role": "user", "content": tool_results})


# ---------------------------------------------------------------------------
# Memory helpers (unchanged from step 2)
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
# Message handler
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    user_text = update.message.text

    if user_id not in conversation_history:
        conversation_history[user_id] = deque(maxlen=10)

    long_term = fetch_memories(user_id)
    system = BASE_SYSTEM_PROMPT + long_term

    conversation_history[user_id].append({"role": "user", "content": user_text})

    # Pass a copy of history to call_claude so the intermediate tool turns
    # (tool_use, tool_result) stay inside call_claude and never enter
    # conversation_history. Only the final plain-text reply goes into history.
    reply = call_claude(list(conversation_history[user_id]), system, user_id)

    conversation_history[user_id].append({"role": "assistant", "content": reply})

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
