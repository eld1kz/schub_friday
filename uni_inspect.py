import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright


load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

LMS_URL = os.environ.get("UNI_LMS_URL", "https://lms.korea.ac.kr/")
AUTH_STATE_PATH = Path(__file__).with_name("uni_auth_state.json")


def print_items(title: str, items: list[str]) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    for item in items[:80]:
        print(item)
    if len(items) > 80:
        print(f"... {len(items) - 80} more")


def main() -> None:
    if not AUTH_STATE_PATH.exists():
        raise SystemExit(
            "Missing uni_auth_state.json. Run python uni_login_test.py and log in first."
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(storage_state=str(AUTH_STATE_PATH))
        page = context.new_page()

        page.goto(LMS_URL, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=15000)

        print(f"Title: {page.title()}")
        print(f"URL: {page.url}")

        links = page.locator("a").evaluate_all(
            """
            els => els
              .map(a => `${(a.innerText || a.textContent || '').trim()} -> ${a.href}`)
              .filter(Boolean)
            """
        )
        buttons = page.locator("button").evaluate_all(
            """
            els => els
              .map(b => (b.innerText || b.textContent || b.getAttribute('aria-label') || '').trim())
              .filter(Boolean)
            """
        )

        print_items("Links", links)
        print_items("Buttons", buttons)

        page.screenshot(path="uni_lms_home.png", full_page=True)
        print("\nSaved screenshot to uni_lms_home.png")
        input("Press Enter to close the browser...")
        browser.close()


if __name__ == "__main__":
    main()
