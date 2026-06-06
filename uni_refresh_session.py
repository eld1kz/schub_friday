"""
Headless re-login: refresh uni_auth_state.json from saved credentials with no
manual interaction, so the daily job can keep the session valid unattended.

    python uni_refresh_session.py

Only saves the session AFTER confirming it authenticates the Canvas API.
Exits non-zero if login fails (wrong creds, or KU added 2FA / CAPTCHA).
"""
import os
import re
import sys
from pathlib import Path

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

LOGIN_URL = os.environ.get("UNI_LOGIN_URL", "https://lms.korea.ac.kr/")
MYLMS_URL = os.environ.get("UNI_MYLMS_URL", "https://mylms.korea.ac.kr/")
USERNAME = os.environ.get("UNI_USERNAME", "")
PASSWORD = os.environ.get("UNI_PASSWORD", "")
AUTH_STATE_PATH = Path(__file__).with_name("uni_auth_state.json")
API_CHECK = MYLMS_URL.rstrip("/") + "/api/v1/users/self/profile"


def close_popup(page) -> None:
    try:
        buttons = page.get_by_role("button", name=re.compile("Close|닫기"))
        for i in reversed(range(buttons.count())):
            try:
                buttons.nth(i).click(timeout=1500)
            except PlaywrightError:
                pass
    except PlaywrightError:
        pass


def main() -> None:
    if not (USERNAME and PASSWORD):
        sys.exit("UNI_USERNAME / UNI_PASSWORD not set in .env")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            close_popup(page)
            # Landing page may be Korean ("포털 계정 로그인 (Portal)"); match on "Portal".
            page.get_by_role("link", name=re.compile("Portal")).click(timeout=15000)
            # SSO form field ids are stable regardless of display language.
            page.locator("#one_id").fill(USERNAME, timeout=15000)
            page.locator("#password").fill(PASSWORD)
            page.locator("#password").press("Enter")
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except PlaywrightTimeoutError:
                pass
            close_popup(page)
            page.goto(MYLMS_URL, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                pass
        except PlaywrightError as e:
            browser.close()
            sys.exit(f"Login failed (selectors changed, or 2FA/CAPTCHA present): {e}")

        # Verify the session truly authenticates the API before overwriting state.
        r = context.request.get(API_CHECK)
        if r.status != 200:
            browser.close()
            sys.exit(f"Login did not yield a valid API session (status {r.status}).")

        context.storage_state(path=str(AUTH_STATE_PATH))
        browser.close()
        print(f"Session refreshed OK -> {AUTH_STATE_PATH.name}")


if __name__ == "__main__":
    main()
