import os
from pathlib import Path

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


def close_lms_popup_if_present(page) -> None:
    close_buttons = page.get_by_role("button", name="Close")
    try:
        if close_buttons.count() > 1:
            close_buttons.nth(1).click(timeout=2000)
        elif close_buttons.count() == 1:
            close_buttons.first.click(timeout=2000)
    except PlaywrightError:
        pass


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        print(f"Opened: {page.title()} ({page.url})")

        did_auto_login = False
        if USERNAME and PASSWORD:
            try:
                close_lms_popup_if_present(page)
                page.get_by_role("link", name="Portal Login").click(timeout=10000)
                page.get_by_role("textbox", name="KUPID Single ID").fill(USERNAME)
                page.get_by_role("textbox", name="Password").fill(PASSWORD)
                page.get_by_role("button", name="Login").click()
                did_auto_login = True
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except PlaywrightTimeoutError:
                    print("Page did not become fully idle; continuing so you can inspect it.")
                close_lms_popup_if_present(page)
            except PlaywrightError as e:
                print("Automatic login did not work with the current selectors.")
                print(f"Reason: {e}")

        if not did_auto_login:
            print("Log in manually in the opened browser.")
            input("After you finish login in the browser, press Enter here to save the session...")

        print("Opening MyLMS before saving session...")
        page.goto(MYLMS_URL, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            print("MyLMS did not become fully idle; continuing so you can inspect it.")

        print(f"Current page: {page.title()} ({page.url})")
        context.storage_state(path=str(AUTH_STATE_PATH))
        print(f"Saved browser session to {AUTH_STATE_PATH}")

        input("Press Enter to close the browser...")
        browser.close()


if __name__ == "__main__":
    main()
