"""Bridge for reading supplier quotes with a Claude Code or Codex session instead of
the OpenAI API (RFQ_QUOTE_PARSER=agent). See docs/AGENT_QUOTE_READING.md.

    python scripts/rfq_agent.py pending [--limit N]      # replies waiting to be read (JSON)
    python scripts/rfq_agent.py submit REPLY_ID FILE     # submit candidate quotes from a JSON file
    python scripts/rfq_agent.py status                   # /rfq/status

The API token comes from SUPPLIER_INTEL_API_TOKEN, else from the linked Railway
service's API_ACCESS_TOKEN. It is never printed. The base URL comes from
SUPPLIER_INTEL_API_URL (default: production).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

BASE = (os.getenv("SUPPLIER_INTEL_API_URL") or "https://supplierintel-production.up.railway.app").rstrip("/")


def token():
    value = os.getenv("SUPPLIER_INTEL_API_TOKEN")
    if value:
        return value
    railway = shutil.which("railway")
    if railway:
        out = subprocess.run([railway, "variables", "--json"], capture_output=True, text=True)
        if out.returncode == 0:
            value = json.loads(out.stdout).get("API_ACCESS_TOKEN")
            if value:
                return value
    sys.exit("No API token: set SUPPLIER_INTEL_API_TOKEN, or run from a folder linked with `railway link`.")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method, headers={
        "Authorization": "Bearer " + token(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        sys.exit(f"HTTP {exc.code}: {detail}")


def main():
    # Replies are often Chinese; Windows consoles default to cp1252.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pending")
    p.add_argument("--limit", type=int, default=20)
    s = sub.add_parser("submit")
    s.add_argument("reply_id", type=int)
    s.add_argument("file")
    sub.add_parser("status")
    args = parser.parse_args()

    if args.cmd == "pending":
        result = call("GET", f"/rfq/replies/pending?limit={args.limit}")
    elif args.cmd == "status":
        result = call("GET", "/rfq/status")
    else:
        with open(args.file, encoding="utf-8") as f:
            payload = json.load(f)
        quotes = payload.get("quotes", payload) if isinstance(payload, dict) else payload
        result = call("POST", f"/rfq/replies/{args.reply_id}/quotes", {"quotes": quotes})
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
