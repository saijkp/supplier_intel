"""One-time Zoho Mail API authorization for the RFQ mail transport.

Run in your own terminal from the project folder (it prompts, so it can't
run inside a non-interactive shell):

    python scripts/zoho_connect.py

Get a Self Client code first at https://api-console.zoho.eu with scopes
ZohoMail.messages.READ,ZohoMail.messages.CREATE,ZohoMail.accounts.READ.
The code expires in minutes, so run this straight after generating it.

Secrets are typed visibly (so paste works on Windows), the screen is cleared
afterwards, nothing is printed, and they are saved straight to the linked
Railway service's variables.
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

ACCOUNTS = "https://accounts.zoho.eu"
MAIL_API = "https://mail.zoho.eu/api/accounts"


def post_form(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST"), timeout=30) as resp:
        return json.load(resp)


def main():
    # Visible input: Windows hidden prompts don't accept Ctrl+V paste.
    # The screen is cleared once the values are read.
    client_id = input("Client ID: ").strip()
    client_secret = input("Client Secret: ").strip()
    code = input("Generated code: ").strip()
    os.system("cls" if os.name == "nt" else "clear")
    if not (client_id and client_secret and code):
        sys.exit("All three values are required.")

    try:
        token = post_form(f"{ACCOUNTS}/oauth/v2/token", {
            "grant_type": "authorization_code", "client_id": client_id,
            "client_secret": client_secret, "code": code,
        })
    except urllib.error.URLError as exc:
        sys.exit(f"Could not reach Zoho: {exc.reason}")
    if "refresh_token" not in token:
        # Zoho returns e.g. {"error": "invalid_code"}; it holds no secret.
        sys.exit(f"Zoho refused the exchange: {token.get('error', 'no refresh token returned')}. "
                 "Generate a fresh code and run this again straight away.")

    request = urllib.request.Request(MAIL_API, headers={"Authorization": "Zoho-oauthtoken " + token["access_token"]})
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            accounts = json.load(resp).get("data") or []
    except urllib.error.HTTPError as exc:
        sys.exit(f"Token works, but the Mail API refused the account lookup (HTTP {exc.code}). "
                 "Your Zoho plan may not include API access.")
    if not accounts:
        sys.exit("Token works, but no mail account was returned.")
    account = accounts[0]
    print(f"Mail API OK for {account.get('primaryEmailAddress')} (account {account.get('accountId')})")

    values = {
        "RFQ_ZOHO_CLIENT_ID": client_id,
        "RFQ_ZOHO_CLIENT_SECRET": client_secret,
        "RFQ_ZOHO_REFRESH_TOKEN": token["refresh_token"],
        "RFQ_ZOHO_ACCOUNT_ID": str(account.get("accountId")),
    }
    # On Windows the npm-installed CLI is railway.cmd, which a bare "railway" can't launch.
    railway = shutil.which("railway")
    result = None
    if railway:
        args = [railway, "variables"]
        for key, value in values.items():
            args += ["--set", f"{key}={value}"]
        result = subprocess.run(args, capture_output=True, text=True)
    if result is None or result.returncode != 0:
        # The code is now spent, so don't lose the token: show it once, in this terminal only.
        print("Could not save to Railway automatically. Add these in the Railway dashboard "
              "(supplier_intel -> Variables), then clear this screen:\n")
        for key, value in values.items():
            print(f"{key}={value}")
        sys.exit(1)
    print("Saved to Railway; the app is restarting with the Zoho connection. Tell Claude it's done.")


if __name__ == "__main__":
    main()
