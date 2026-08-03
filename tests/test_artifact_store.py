"""
tests/test_artifact_store.py

Tests for collection/artifact_store.py -- filesystem layout for
Collection Service's HTML/screenshot/download/JSON artifacts. Every
test points base_dir at a tmp_path, never the real
config.settings.COLLECTION_ARTIFACTS_DIR.
"""

from __future__ import annotations

import json

from collection.artifact_store import ArtifactStore


class TestNewRunDir:

    def test_creates_and_returns_the_directory(self, tmp_path):
        store = ArtifactStore(base_dir=tmp_path)
        run_dir = store.new_run_dir(supplier_id=42, run_id="20260803T000000Z")
        assert run_dir.exists()
        assert run_dir == tmp_path / "42" / "20260803T000000Z"

    def test_creates_a_downloads_subdirectory(self, tmp_path):
        store = ArtifactStore(base_dir=tmp_path)
        run_dir = store.new_run_dir(supplier_id=42, run_id="20260803T000000Z")
        assert (run_dir / "downloads").exists()

    def test_run_id_defaults_to_a_timestamp_when_omitted(self, tmp_path):
        store = ArtifactStore(base_dir=tmp_path)
        run_dir = store.new_run_dir(supplier_id=42)
        assert run_dir.parent == tmp_path / "42"
        assert run_dir.name  # non-empty, some timestamp string


class TestRelativePath:

    def test_relative_path_is_portable_across_base_dirs(self, tmp_path):
        store = ArtifactStore(base_dir=tmp_path)
        run_dir = store.new_run_dir(supplier_id=42, run_id="20260803T000000Z")
        assert store.relative_path(run_dir) == "42/20260803T000000Z"


class TestSaveMethods:

    def test_save_html_writes_readable_content(self, tmp_path):
        store = ArtifactStore(base_dir=tmp_path)
        run_dir = store.new_run_dir(supplier_id=1, run_id="run1")
        path = store.save_html(run_dir, 0, "https://acme.example.com/about", "<html>hi</html>")
        assert path.exists()
        assert path.read_text(encoding="utf-8") == "<html>hi</html>"
        assert path.parent == run_dir
        assert path.suffix == ".html"

    def test_save_screenshot_writes_bytes(self, tmp_path):
        store = ArtifactStore(base_dir=tmp_path)
        run_dir = store.new_run_dir(supplier_id=1, run_id="run1")
        path = store.save_screenshot(run_dir, 0, "https://acme.example.com", b"\x89PNG fake bytes")
        assert path.exists()
        assert path.read_bytes() == b"\x89PNG fake bytes"
        assert path.suffix == ".png"

    def test_save_download_goes_into_downloads_subdir(self, tmp_path):
        store = ArtifactStore(base_dir=tmp_path)
        run_dir = store.new_run_dir(supplier_id=1, run_id="run1")
        path = store.save_download(run_dir, "Product Catalogue 2026.pdf", b"%PDF-fake")
        assert path.parent == run_dir / "downloads"
        assert path.suffix == ".pdf"
        assert path.read_bytes() == b"%PDF-fake"

    def test_save_extracted_json_round_trips(self, tmp_path):
        store = ArtifactStore(base_dir=tmp_path)
        run_dir = store.new_run_dir(supplier_id=1, run_id="run1")
        path = store.save_extracted_json(run_dir, {"emails": ["a@b.com"], "count": 3})
        assert json.loads(path.read_text(encoding="utf-8")) == {"emails": ["a@b.com"], "count": 3}

    def test_different_pages_produce_different_filenames(self, tmp_path):
        store = ArtifactStore(base_dir=tmp_path)
        run_dir = store.new_run_dir(supplier_id=1, run_id="run1")
        path_a = store.save_html(run_dir, 0, "https://acme.example.com/", "a")
        path_b = store.save_html(run_dir, 1, "https://acme.example.com/about", "b")
        assert path_a != path_b
