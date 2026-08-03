# Deploying the API layer

> See `README.md` for what this system does, its known limitations,
> and the roadmap — this file is deployment and verification steps
> only.

`api/app.py` is a FastAPI wrapper around the existing pipeline and
query functions — everything in `pipeline/orchestrator.py` and
`storage/repository.py` is unchanged; this just exposes it over HTTP
so a separate frontend (e.g. hosted on Netlify) can call it.

## Local development

```bash
pip install -r requirements.txt
python main.py init-db
export API_ACCESS_TOKEN=dev-token
export ALLOWED_ORIGINS=http://localhost:3000
uvicorn api.app:app --reload
```

Visit `http://127.0.0.1:8000/docs` for interactive API docs (Swagger
UI, generated automatically from `api/models.py` — genuinely useful
for whoever builds the frontend, or for testing by hand without
Postman).

## Deploying to Railway — detailed walkthrough

### Step 1: Get the code onto GitHub

```bash
cd supplier_intel
git init
git add .
git commit -m "Initial commit"
```
Create a private repo on GitHub, then:
```bash
git remote add origin https://github.com/<you>/supplier_intel.git
git push -u origin main
```

### Step 2: Create the Railway project

1. railway.app → New Project → "Deploy from GitHub repo" → pick the repo.
2. Railway auto-detects Python from `requirements.txt` — no `Procfile` needed if you set the start command in Step 5.
3. Railway's default builder for this repo is **Railpack** (not the older Nixpacks — a `nixpacks.toml` at the repo root is silently ignored under Railpack, confirmed the hard way). `pip install -r requirements.txt` alone installs the `playwright` Python package but not the actual browser binary `collection.SiteCollector` needs. Railpack's own build log names the fix directly: set the env var `RAILPACK_PYTHON_PLAYWRIGHT_INSTALL=1` (Step 4 below) and it downloads Chromium during the build automatically. Without this, Collection Service fails on first *use*, not at build time — a silent trap if you don't check for it. Adds real build time (~1-2 min) and image size (~300MB) — expected, not a build error. Confirm it worked by checking build logs for `chromium` download lines.

### Step 3: Add persistent storage

1. In the service → **Settings → Volumes → New Volume**.
2. Mount path: `/data`.
3. Without this step, everything works right up until the next deploy, when the whole database silently vanishes — Railway's filesystem is otherwise ephemeral. This is the single most important step to not skip.

### Step 4: Environment variables

Service → **Variables** → add each of these:

| Variable | Value | Required? |
|---|---|---|
| `SUPPLIER_INTEL_DB_PATH` | `/data/suppliers.db` | Yes |
| `RAILWAY_RUN_UID` | `0` | Yes — volumes mount as root; without this a non-root container gets a permission error writing to `/data` |
| `API_ACCESS_TOKEN` | a long random string (`openssl rand -hex 32`) | Yes — API refuses all requests without it |
| `ALLOWED_ORIGINS` | `https://your-frontend.netlify.app` | Yes, once you have a frontend |
| `APIFY_TOKEN`, `QICHACHA_API_KEY`, `QICHACHA_SECRET_KEY`, `SERPAPI_KEY`, `OPENAI_API_KEY`, `GOOGLE_PLACES_API_KEY`, `AMAP_API_KEY` | your real keys | Only for the integrations you actually want live — anything left unset is skipped gracefully everywhere in this codebase, never silently broken |
| `COLLECTION_ARTIFACTS_DIR` | `/data/collection` | Yes, once you use Collection Service — same reasoning as `SUPPLIER_INTEL_DB_PATH`: only `/data` survives a redeploy, and `config.settings.DATA_DIR` itself has no env override, so Collection Service's HTML/screenshot artifacts are silently wiped on every deploy without this |
| `RAILPACK_PYTHON_PLAYWRIGHT_INSTALL` | `1` | Yes, once you use Collection Service — this is the actual mechanism that downloads the Chromium binary during build under Railway's Railpack builder (see Step 2 above). Without it, `playwright` (the Python package) installs fine but Collection Service fails at runtime with no browser to launch. |
| `WEBSHARE_PROXY_USERNAME`, `WEBSHARE_PROXY_PASSWORD` | your Webshare proxy credentials | Only if you want Collection Service routed through a rotating proxy — unset means direct connection (`COLLECTION_PROXY_PROVIDER=none`, the default) |
| `COLLECTION_PROXY_PROVIDER` | `webshare` | Only to actually enable the proxy above — Webshare is the only provider implemented so far (see `collection/proxy_provider.py`) |
| `COLLECTION_PAGE_TIMEOUT_MS`, `COLLECTION_JOB_MAX_SECONDS`, `COLLECTION_MAX_CONCURRENT_JOBS` | defaults are `25000`, `1200`, `1` | No — sensible defaults; only override if you've confirmed a `collect --pending` batch needs a different budget |

**Memory**: headless Chromium needs real headroom — a `collect`/`POST /collection/jobs` call that OOMs is a memory-plan problem, not a code bug. Recommend at least a 1GB Railway plan before relying on Collection Service at any real volume.

### Step 5: Start command

Service → **Settings → Deploy → Start Command**:

```
uvicorn api.app:app --host 0.0.0.0 --port $PORT
```

Railway injects `$PORT` automatically — don't hardcode a port number.

### Step 6: Deploy and verify

Railway deploys on push automatically once connected. Watch the build logs for:

```
Applied schema migration v8: pipeline_jobs table ...
INFO:     Uvicorn running on http://0.0.0.0:xxxx
```

That confirms the volume is writable and the app started. Then:

```bash
curl https://<your-app>.up.railway.app/health
# {"status":"ok"}
```

If that doesn't return immediately, check Railway's logs before anything else — a missing `RAILWAY_RUN_UID=0` shows up here as a database-write permission error on first request.

## Verifying every integration actually works

`doctor --live` exists specifically for this — not "is a key present," but "does it actually work." Run it against the deployed environment (Railway's own shell, via the project dashboard's "Connect" → shell, so it has real access to the volume and env vars):

```bash
python main.py doctor --live
```

It makes one minimal real call per configured integration and reports:

- **pass** — genuinely works
- **fail** — configured but broken (bad key, expired token, IP blocked) — shown with a specific reason
- **skipped** — not configured at all, not an error

It exits non-zero if anything failed, so it's safe to wire into a CI health check later. Cost is minimal by design (one account-info call for Apify, one tiny completion for OpenAI, etc.) but not zero — don't loop it.

**What it already found, before you've even deployed anywhere:** HKTDC, ImportYeti, and several exhibition-directory sources return HTTP 403 from this environment's IP — likely anti-bot blocking of datacenter traffic. Railway's IPs are datacenter IPs too, so expect these specific sources to keep failing there unless you add a residential/rotating proxy (Bright Data, Smartproxy) in front of them. This isn't a bug to fix in the code — it's a real, disclosed limitation of scraping those particular sites from any cloud host.

### Manual smoke test, end to end

```bash
TOKEN=<your API_ACCESS_TOKEN>
URL=https://<your-app>.up.railway.app

curl $URL/health

curl -H "Authorization: Bearer $TOKEN" "$URL/suppliers/search?product=wheel"

curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query":"wheel bearings","run_capability_extraction":false}' \
  $URL/pipeline/jobs
# note the "id" in the response, then:

curl -H "Authorization: Bearer $TOKEN" "$URL/pipeline/jobs/<id>"
# poll every few seconds until "status": "completed"

# Collection Service -- pick a real supplier id that has a domain on file first
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"supplier_id": 1}' \
  $URL/collection/jobs
# poll the returned job id the same way; check collection_runs/artifacts_dir once complete
```

### Running the test suite against the deployed environment

The 674 automated tests all run against a temporary local SQLite file, not your production database — safe to run anywhere, including in a Railway shell, without touching real data:

```bash
python -m pytest -q
```

A green run here confirms the *logic* is correct. It does not confirm the *live* integrations work — that's what `doctor --live` is for. You need both: tests prove the code is right, `doctor --live` proves the deployed environment is configured right.

## Wiring up a Netlify frontend

The frontend calls the Railway API directly over HTTPS — Netlify
itself never runs any of this Python code. Every request needs:

```
Authorization: Bearer <API_ACCESS_TOKEN>
```

Core endpoints:

| Method | Path | Purpose |
|---|---|---|
| GET | `/suppliers/search` | The main query — `product`, `require` (repeatable), `manufacturers_only`, `country`, `min_score` |
| GET | `/suppliers/{id}` | Full detail incl. matched capabilities |
| POST | `/pipeline/jobs` | Trigger a pipeline run — returns immediately with a job id |
| POST | `/pipeline/enrichment-jobs` | Trigger find-websites/extract-capabilities/verify-facilities across every existing supplier needing it |
| POST | `/collection/jobs` | Trigger Collection Service (real headless browser) against one supplier or a batch — same job/poll pattern |
| POST | `/verification/jobs` | Trigger Verification Service (AI cross-check, `ai_confidence_score`) against one supplier or a batch |
| POST | `/discovery/jobs` | Trigger Discovery Service — AI-assisted supplier search, grounded entirely in real SerpAPI results |
| POST | `/suppliers/{id}/reverify` | Re-collect then re-verify one already-known supplier — changes append to its change log, never silently overwritten |
| GET | `/suppliers/{id}/verification-history` | Every verification_ai run for one supplier, newest first |
| GET | `/suppliers/{id}/change-log` | Every field-level change collection/verification_ai have made to one supplier, newest first |
| GET | `/pipeline/jobs/{id}` | Poll job status (`queued` -> `running` -> `completed`/`failed`) — shared by every job type above |
| GET | `/export/csv` | Download results as CSV |
| GET | `/export/excel` | Download results as `.xlsx`, with contact/address enrichment columns CSV omits |
| GET | `/health` | No auth — for uptime checks |

`POST /pipeline/jobs` returns immediately (202) because a full run
with capability extraction can take minutes — the frontend should
poll `GET /pipeline/jobs/{id}` every few seconds until `status` is
`completed` or `failed`, not wait on the original request.

## Known limitation, disclosed rather than hidden

Job execution runs in-process via FastAPI's `BackgroundTasks`, not a
real task queue. This is fine for a single Railway instance. If this
ever needs to run across multiple replicas, `BackgroundTasks` breaks
silently — a job created on instance A will never run if instance B
is the one that happens to poll for it. `storage.repository`'s
`pipeline_jobs` methods are the thing to replace with a real queue
(Celery + Redis, or Railway's own queue primitives) if that need
arises; nothing else in the API needs to change.
