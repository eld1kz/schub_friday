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
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from google_auth_oauthlib.flow import Flow
from supabase import create_client
import uvicorn

from health_repository import SupabaseHealthRepository
from health_service import HealthService

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

app = FastAPI()
db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
health_service = HealthService(SupabaseHealthRepository(db))

REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:8080/oauth/callback")
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

# Friendly labels for the scopes we request, shown on the success page so the
# user sees everything they actually connected (not just Calendar).
SCOPE_LABELS = {
    "https://www.googleapis.com/auth/calendar": "Google Calendar",
    "https://www.googleapis.com/auth/gmail.readonly": "Gmail (read)",
    "https://www.googleapis.com/auth/gmail.send": "Gmail (send)",
}

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

        # List what the user actually granted (Google may return a subset).
        granted = creds.scopes or SCOPES
        items = "".join(f"<li>{SCOPE_LABELS.get(s, s)}</li>" for s in granted)
        return HTMLResponse(f"""
            <html><body style="font-family:sans-serif;text-align:center;padding:60px">
            <h2>Connected!</h2>
            <p>You've connected:</p>
            <ul style="display:inline-block;text-align:left">{items}</ul>
            <p>You can close this tab and return to Telegram.</p>
            </body></html>
        """)

    except Exception:
        tb = traceback.format_exc()
        print("\n[oauth] CALLBACK ERROR:\n" + tb)          # full trace in terminal
        return HTMLResponse(
            f"<pre style='color:red'>{tb}</pre>", status_code=500
        )


@app.post("/health/ingest")
async def health_ingest(request: Request, x_health_token: str = Header(default="")):
    """
    Receives daily Apple Health metrics POSTed by the iPhone Shortcut.

    Auth: an `X-Health-Token` header must match HEALTH_INGEST_TOKEN. The user is
    taken from the payload's `user_id`, falling back to TELEGRAM_CHAT_ID (this is
    a single-user personal bot). A re-POST for the same day overwrites that day.
    """
    expected = os.environ.get("HEALTH_INGEST_TOKEN", "")
    if not expected or x_health_token != expected:
        raise HTTPException(status_code=401, detail="bad token")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="body must be JSON")

    try:
        row = health_service.ingest(payload, os.environ.get("TELEGRAM_CHAT_ID", ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"ok": True, "date": row.get("metric_date")}


if __name__ == "__main__":
    # Railway sets $PORT and routes its public domain to 0.0.0.0:$PORT.
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8080")))
