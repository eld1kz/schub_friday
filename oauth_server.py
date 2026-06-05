"""
OAuth 2.0 callback server for Google Calendar.
Run this in a separate terminal alongside the bot:
    python oauth_server.py

It listens on http://localhost:8080 and handles step 4-5 of the OAuth flow:
Google redirects the user's browser here after they approve access, and we
exchange the one-time authorization code for durable tokens.
"""
import os
import traceback
# Both must be set before any google_auth_oauthlib import.
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"   # allow http for localhost
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"    # don't error if Google returns scopes in a different order/format

from datetime import timezone
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from google_auth_oauthlib.flow import Flow
from supabase import create_client
import uvicorn

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

app = FastAPI()
db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

REDIRECT_URI = "http://localhost:8080/oauth/callback"
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

CLIENT_CONFIG = {
    "web": {
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "redirect_uris": [REDIRECT_URI],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}


@app.get("/oauth/callback")
async def oauth_callback(code: str, state: str):
    """
    Google calls this endpoint after the user approves access.

    `code`  — a one-time authorization code (expires in minutes).
    `state` — the Telegram user_id we embedded in the auth URL.

    We exchange the code for tokens here. The exchange is a POST request
    from our server to Google's token endpoint — the user's browser is
    no longer involved at this point.
    """
    try:
        flow = Flow.from_client_config(CLIENT_CONFIG, scopes=SCOPES, redirect_uri=REDIRECT_URI,
                                       autogenerate_code_verifier=False)
        flow.fetch_token(code=code)
        creds = flow.credentials

        expiry = creds.expiry
        if expiry is not None and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)

        db.table("google_tokens").upsert({
            "user_id": state,
            "access_token": creds.token,
            "refresh_token": creds.refresh_token,
            "expiry": expiry.isoformat() if expiry else None,
        }).execute()

        return HTMLResponse("""
            <html><body style="font-family:sans-serif;text-align:center;padding:60px">
            <h2>Google Calendar connected!</h2>
            <p>You can close this tab and return to Telegram.</p>
            </body></html>
        """)

    except Exception:
        tb = traceback.format_exc()
        print("\n[oauth] CALLBACK ERROR:\n" + tb)          # full trace in terminal
        return HTMLResponse(
            f"<pre style='color:red'>{tb}</pre>", status_code=500
        )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
