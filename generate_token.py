"""
Run this ONCE on your own laptop (not on the server).

    pip install google-auth-oauthlib
    python tools/generate_token.py

It opens a browser, you approve, and it writes token.json next to itself.
Paste the whole contents of that file into the GOOGLE_TOKEN_JSON environment
variable on Render. Do not commit token.json to git.
"""

import json
import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT_SECRETS = os.path.join(HERE, "..", "credentials.json")
OUT = os.path.join(HERE, "..", "token.json")

if not os.path.exists(CLIENT_SECRETS):
    sys.exit(
        "credentials.json not found in the project root.\n"
        "Google Cloud Console > Credentials > Create OAuth client ID > "
        "Desktop app > Download JSON > rename to credentials.json"
    )

flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS, SCOPES)
creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

with open(OUT, "w", encoding="utf-8") as fh:
    fh.write(creds.to_json())

data = json.loads(creds.to_json())
print("\ntoken.json written to", os.path.abspath(OUT))
if not data.get("refresh_token"):
    print(
        "\nWARNING: no refresh_token in the response. Revoke the app at "
        "https://myaccount.google.com/permissions and run this again."
    )
else:
    print("refresh_token present. Copy the file contents into GOOGLE_TOKEN_JSON.")
print(
    "\nReminder: if your OAuth consent screen is still in 'Testing', this "
    "refresh token dies in 7 days. Set it to 'In production' first."
)
