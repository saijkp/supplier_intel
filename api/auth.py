"""
api/auth.py

Simple shared-secret auth for v1: a single `API_ACCESS_TOKEN` (an
environment variable), checked on every route that reads supplier data
or triggers a paid pipeline run. This is deliberately not per-user
auth -- there are no external users yet, just you and a frontend you
control. The moment this API has real external users with different
permission levels, this is the first thing to replace, not extend.

Why this exists at all
-----------------------
Without it, anyone who finds the deployed URL can trigger
`pipeline.run()` (real OpenAI/SerpAPI/Google Places/Amap spend per
call) or read the assembled supplier database for free. A single
shared token is the minimum viable protection against that -- not a
complete auth system, and not meant to be one.

Fails closed, not open
------------------------
If `API_ACCESS_TOKEN` isn't set, every protected route returns 503,
not 200. The alternative -- silently allowing every request through
when no token is configured -- is the kind of default that looks fine
in local development and quietly leaves a production deployment wide
open. A misconfiguration should be loud and immediately visible, not
a silent security hole.
"""

from __future__ import annotations

from fastapi import Header, HTTPException

from config.settings import API_ACCESS_TOKEN


async def require_api_token(authorization: str = Header(default="")) -> None:
    """FastAPI dependency: raises 503 if the server has no token
    configured, 401 if the caller's token doesn't match, and returns
    None (permitting the request) otherwise.

    Expects `Authorization: Bearer <token>`.
    """
    if not API_ACCESS_TOKEN:
        raise HTTPException(
            status_code=503,
            detail=(
                "API_ACCESS_TOKEN is not configured on the server -- refusing to serve "
                "requests rather than silently allowing everyone through. Set it as an "
                "environment variable before deploying this publicly."
            ),
        )

    expected = f"Bearer {API_ACCESS_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API token")
