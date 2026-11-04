"""Drive the Supplier Intel production API from a terminal agent (Codex or Claude Code).
See AGENTS.md for the workflows and the rules about cost and sending.

    python scripts/si.py get  /suppliers/search?product=kitchen+cabinets&country=China
    python scripts/si.py post /rfq/campaigns '{"title": "...", "product": "...", "supplier_ids": [1, 2]}'
    python scripts/si.py post /pipeline/jobs @body.json          # body from a file
    python scripts/si.py wait JOB_ID                            # poll a pipeline job until done
    python scripts/si.py run  /pipeline/jobs '{"query": "..."}'  # post, then wait for the job it starts
    python scripts/si.py routes                                 # every route with its summary

The token comes from SUPPLIER_INTEL_API_TOKEN, else from the linked Railway service's
API_ACCESS_TOKEN (same as scripts/rfq_agent.py). It is never printed. The base URL comes
from SUPPLIER_INTEL_API_URL (default: production).
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

from rfq_agent import BASE, token

_TOKEN = None


def call(method, path, body=None):
    global _TOKEN
    if _TOKEN is None:
        _TOKEN = token()
    # Git Bash/MSYS rewrites a leading "/rfq/status" into "C:/Program Files/Git/rfq/status".
    if len(path) > 2 and path[1] == ":" and "/Git/" in path:
        path = path.split("/Git", 1)[1]
    if not path.startswith("/"):
        path = "/" + path
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method, headers={
        "Authorization": "Bearer " + _TOKEN, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        sys.exit(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:1000]}")
    try:
        return json.loads(raw)
    except ValueError:
        return raw  # CSV exports and other plain-text responses


def load_body(text):
    if text is None:
        return {}
    if text.startswith("@"):
        with open(text[1:], encoding="utf-8") as f:
            return json.load(f)
    return json.loads(text)


def wait(job_id, interval=15):
    while True:
        job = call("GET", f"/pipeline/jobs/{job_id}")
        if job.get("status") in ("completed", "failed"):
            return job
        progress = job.get("progress")
        print(f"... {job.get('status')} {json.dumps(progress, ensure_ascii=False)[:200] if progress else ''}",
              file=sys.stderr, flush=True)
        time.sleep(interval)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # supplier names and replies are often Chinese
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("get").add_argument("path")
    sub.add_parser("delete").add_argument("path")
    for name in ("post", "patch", "put", "run"):
        p = sub.add_parser(name)
        p.add_argument("path")
        p.add_argument("body", nargs="?")
    sub.add_parser("wait").add_argument("job_id")
    sub.add_parser("routes")
    args = parser.parse_args()

    if args.cmd == "routes":
        spec = call("GET", "/openapi.json")
        result = [f"{m.upper():6} {p}  {o.get('summary', '')}" for p, ops in spec["paths"].items() for m, o in ops.items()]
        print("\n".join(result))
        return
    if args.cmd in ("get", "delete"):
        result = call(args.cmd.upper(), args.path)
    elif args.cmd == "wait":
        result = wait(args.job_id)
    else:
        method = "POST" if args.cmd == "run" else args.cmd.upper()
        result = call(method, args.path, load_body(args.body))
        if args.cmd == "run":
            job_id = (result.get("id") or result.get("job_id")) if isinstance(result, dict) else None
            if not job_id:
                sys.exit(f"No job id in response: {json.dumps(result)[:500]}")
            result = wait(job_id)
    print(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
