"""
collection/artifact_store.py

Filesystem layout for Collection Service's HTML/screenshot/PDF/JSON
artifacts, under config.settings.COLLECTION_ARTIFACTS_DIR (which is
itself env-var-driven, following DB_PATH's own pattern, specifically
so these survive a Railway redeploy -- see that setting's own comment
for why COLLECTION_ARTIFACTS_DIR exists at all).

Layout: {COLLECTION_ARTIFACTS_DIR}/{supplier_id}/{run_id}/
    page_00_<slug>.html
    page_00_<slug>.png
    downloads/<filename>
    extracted.json

`collection_runs.artifacts_dir` stores the path *relative* to
COLLECTION_ARTIFACTS_DIR (e.g. "42/20260803T120000Z"), never an
absolute path -- so it stays portable across environments where
COLLECTION_ARTIFACTS_DIR itself differs (a developer's local `data/
collection` vs. Railway's `/data/collection`).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config.settings import COLLECTION_ARTIFACTS_DIR

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, *, max_len: int = 40) -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return (slug or "page")[:max_len]


class ArtifactStore:

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = Path(base_dir) if base_dir is not None else COLLECTION_ARTIFACTS_DIR

    def new_run_dir(self, supplier_id: int, run_id: Optional[str] = None) -> Path:
        """Creates and returns the absolute directory for one collection
        run. `run_id` defaults to a UTC timestamp; pass an explicit
        value in tests for deterministic paths."""
        run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = self.base_dir / str(supplier_id) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "downloads").mkdir(exist_ok=True)
        return run_dir

    def relative_path(self, run_dir: Path) -> str:
        """The path to store in collection_runs.artifacts_dir -- relative
        to base_dir, portable across environments."""
        return str(run_dir.relative_to(self.base_dir)).replace("\\", "/")

    def save_html(self, run_dir: Path, page_index: int, url: str, html: str) -> Path:
        path = run_dir / f"page_{page_index:02d}_{_slugify(url)}.html"
        path.write_text(html, encoding="utf-8")
        return path

    def save_screenshot(self, run_dir: Path, page_index: int, url: str, png_bytes: bytes) -> Path:
        path = run_dir / f"page_{page_index:02d}_{_slugify(url)}.png"
        path.write_bytes(png_bytes)
        return path

    def save_download(self, run_dir: Path, filename: str, content: bytes) -> Path:
        safe_name = _slugify(Path(filename).stem) + Path(filename).suffix
        path = run_dir / "downloads" / safe_name
        path.write_bytes(content)
        return path

    def save_extracted_json(self, run_dir: Path, data: Any) -> Path:
        path = run_dir / "extracted.json"
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        return path
