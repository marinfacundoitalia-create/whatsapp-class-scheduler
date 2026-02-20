"""
One-time setup script for Google Calendar OAuth2 authentication.
Run this locally BEFORE deploying to generate token.json.

Usage:
    1. Download credentials.json from Google Cloud Console
    2. Place it in the project root
    3. Run: python execution/setup_google_auth.py
    4. A browser window will open for Google sign-in
    5. token.json will be created in the project root

After this, the bot can use token.json for Calendar API access.
The token auto-refreshes, so you only need to run this once.
"""

import sys
from pathlib import Path

# Add project root to path so we can import from execution/
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from execution.google_calendar import authenticate_calendar, TOKEN_FILE, CREDENTIALS_FILE


def main():
    print("=" * 50)
    print("Google Calendar OAuth2 Setup")
    print("=" * 50)

    # Check credentials.json exists
    if not CREDENTIALS_FILE.exists():
        print(f"\n[ERROR] credentials.json not found at:\n   {CREDENTIALS_FILE}")
        print("\nTo fix this:")
        print("1. Go to https://console.cloud.google.com/")
        print("2. Create a project (or select existing)")
        print("3. Enable 'Google Calendar API'")
        print("4. Go to Credentials -> Create Credentials -> OAuth client ID")
        print("5. Application type: Desktop app")
        print("6. Download the JSON and save as 'credentials.json' in project root")
        sys.exit(1)

    print(f"\n[OK] Found credentials.json at:\n   {CREDENTIALS_FILE}")

    # Check if token already exists
    if TOKEN_FILE.exists():
        print(f"\n[WARNING] token.json already exists at:\n   {TOKEN_FILE}")
        response = input("   Overwrite? (y/N): ").strip().lower()
        if response != "y":
            print("   Keeping existing token. Done.")
            return

    print("\n[AUTH] Starting OAuth2 flow...")
    print("   A browser window will open. Sign in with Google and grant access.\n")

    try:
        service = authenticate_calendar()
        # Quick test: list next event
        events = service.events().list(
            calendarId="primary", maxResults=1, singleEvents=True, orderBy="startTime",
            timeMin="2020-01-01T00:00:00Z"
        ).execute()

        print(f"\n[OK] Authentication successful!")
        print(f"   token.json saved at: {TOKEN_FILE}")

        items = events.get("items", [])
        if items:
            print(f"   Calendar access verified. Found event: '{items[0].get('summary', 'Untitled')}'")
        else:
            print("   Calendar access verified. No upcoming events found.")

    except Exception as e:
        print(f"\n[ERROR] Authentication failed: {e}")
        sys.exit(1)

    print("\n[OK] Setup complete! You can now run the bot.")


if __name__ == "__main__":
    main()
