"""Interactive Zoho Self Client authorization; run manually from linked PowerShell.

No credentials are printed or placed in command arguments. This never sends mail.
"""
import argparse
from getpass import getpass
import shutil
import subprocess
import sys

import httpx

SCOPES = 'ZohoMail.accounts.READ,ZohoMail.folders.READ,ZohoMail.messages.READ,ZohoMail.messages.CREATE'
REGIONS = ('eu', 'com', 'in', 'com.au', 'jp')


def authorize(region, address, client_id, client_secret, code, client):
    """Injectable HTTP client; never include provider responses in exceptions."""
    step = 'Step A (exchanging the grant code)'
    hint = ''
    try:
        response = client.post(f'https://accounts.zoho.{region}/oauth/v2/token', data={
            'grant_type': 'authorization_code', 'client_id': client_id,
            'client_secret': client_secret, 'code': code})
        response.raise_for_status()
        tokens = response.json()
        if not tokens.get('refresh_token') or not tokens.get('access_token'):
            # Zoho returns HTTP 200 with an error code (e.g. invalid_code, invalid_client).
            # The code itself is safe to show; it contains no credential.
            hint = f" Zoho said: {str(tokens.get('error') or 'no refresh token returned')[:60]}."
            raise ValueError()
        step = 'Step B (reading the mailbox list)'
        response = client.get(f'https://mail.zoho.{region}/api/accounts', headers={
            'Authorization': 'Zoho-oauthtoken ' + tokens['access_token']})
        response.raise_for_status()
        accounts = response.json().get('data', [])
        step = f'Step C (finding {address} in this Zoho login)'
        def addresses(a):
            found = {str(a.get('primaryEmailAddress', '')).lower(), str(a.get('mailboxAddress', '')).lower()}
            found |= {str(e.get('mailId', '')).lower() for e in a.get('emailAddress', []) if isinstance(e, dict)}
            return found
        account = next(a for a in accounts if address.lower() in addresses(a))
        if not str(account['accountId']).isdigit():
            raise ValueError()
        return {'REGION': region, 'ACCOUNT_ID': str(account['accountId']),
                'CLIENT_ID': client_id, 'CLIENT_SECRET': client_secret,
                'REFRESH_TOKEN': tokens['refresh_token']}
    except Exception:
        raise ValueError(f'Authorization failed at {step}.{hint} Check the selected account, region, scopes and fresh grant code. No Railway variables were changed.') from None


def save_variables(railway, values, run=subprocess.run):
    # --stdin keeps values out of process listings; discard CLI output too.
    for key, value in values.items():
        result = run([railway, 'variables', 'set', '--skip-deploys', '--stdin', key],
                     input=value, text=True, capture_output=True)
        if result.returncode:
            raise ValueError('Railway save failed; some variables may have been saved. Check railway link and rerun before redeploying.')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mailbox', choices=['rfq', 'customer', 'outreach'], default='rfq')
    p.add_argument('--region', choices=REGIONS, default='eu')
    args = p.parse_args()
    railway = shutil.which('railway')
    if not railway:
        p.error('Install the Railway CLI, sign in with railway login, then use railway link to select Supplier Intel production.')
    # rfq = suppliers (sai@ikeng.online); customer = sai@ikeng.co.uk; outreach = marketing (sj@ikeng.online)
    address, prefix = {'customer': ('sai@ikeng.co.uk', 'CUSTOMER_ZOHO_'),
                       'outreach': ('sj@ikeng.online', 'OUTREACH_ZOHO_')}.get(args.mailbox, ('sai@ikeng.online', 'RFQ_ZOHO_'))
    print(f'1. In this PowerShell folder, railway link must point to the Supplier Intel production service.')
    print(f'2. Open https://api-console.zoho.{args.region}/ in your browser. Sign in as {address}.')
    print('3. Create or open a Self Client. Copy its client ID and client secret. Paste them below (input is hidden).')
    client_id = getpass('Client ID: ').strip()
    print(f'   Client ID received: {len(client_id)} characters, starts with "{client_id[:5]}"'
          ' (a Zoho client ID is usually 35 characters starting "1000.").')
    secret = getpass('Client secret: ').strip()
    print(f'   Client secret received: {len(secret)} characters (usually 42).')
    print('4. In Generate Code, use these scopes (including folders.READ for the Sent checks):')
    print(SCOPES)
    print('Choose a 10-minute duration, enter Supplier Intel as the description, then generate the code.')
    code = getpass('Paste the new grant code: ').strip()
    try:
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            values = authorize(args.region, address, client_id, secret, code, client)
        values = {prefix + key: value for key, value in values.items()}
        if args.mailbox in ('customer', 'outreach'):
            values[prefix + 'ADDRESS'] = address
        print(f'5. Verified {address}. Ready to save only {prefix} variables to your linked Railway service.')
        if input('Type SAVE to save these variables (no deploy and no email): ').strip() != 'SAVE':
            print('Cancelled; no Railway variables changed.')
            return
        save_variables(railway, values)
        print('6. Saved. Redeploy Supplier Intel when ready, then use Customers > Check inbox. Sending still requires human approval and Send.')
    except ValueError as exc:
        sys.exit(str(exc))


if __name__ == '__main__':
    main()
