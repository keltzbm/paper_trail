import os

from playwright.sync_api import sync_playwright

AUTH_FILE = "auth.json"


def create_auth_session():
    with sync_playwright() as p:
        # Launch non-headless so you can interact with the page
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        print("Navigating to Springer Link...")
        page.goto("https://link.springer.com")

        print("\n=======================================================")
        print("ACTION REQUIRED:")
        print("1. Click 'Log in' in the browser window.")
        print("2. Choose 'Access via your institution' (OpenAthens/Shibboleth).")
        print("3. Complete your university login / 2FA.")
        print("4. Verify you see 'Access provided by [Your University]' on Springer.")
        print("=======================================================\n")

        input("Press ENTER in this terminal once you have successfully logged in...")

        # Save session cookies and local storage to auth.json
        context.storage_state(path=AUTH_FILE)
        print(f"--> Saved authentication context to '{os.path.abspath(AUTH_FILE)}'")

        browser.close()


if __name__ == "__main__":
    create_auth_session()
