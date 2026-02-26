"""
Verify Google Sheets API connectivity.
Writes a test value to cell A1 and reads it back.
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

KEY_PATH = os.getenv("GOOGLE_SERVICE_ACCOUNT_KEY_PATH")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

if not KEY_PATH or not SHEET_ID:
    print("ERROR: GOOGLE_SERVICE_ACCOUNT_KEY_PATH and GOOGLE_SHEET_ID must be set in .env")
    sys.exit(1)

from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def main():
    creds = service_account.Credentials.from_service_account_file(KEY_PATH, scopes=SCOPES)
    service = build("sheets", "v4", credentials=creds)
    sheet = service.spreadsheets()

    test_value = "verify_ok"
    range_ = "Sheet1!A1"

    # Write
    sheet.values().update(
        spreadsheetId=SHEET_ID,
        range=range_,
        valueInputOption="RAW",
        body={"values": [[test_value]]},
    ).execute()

    # Read back
    result = sheet.values().get(spreadsheetId=SHEET_ID, range=range_).execute()
    read_back = result.get("values", [[None]])[0][0]

    if read_back == test_value:
        print(f"✓ Google Sheets: wrote '{test_value}' to A1, read back: '{read_back}'")
    else:
        print(f"✗ Mismatch — wrote '{test_value}', read back '{read_back}'")
        sys.exit(1)

if __name__ == "__main__":
    main()
