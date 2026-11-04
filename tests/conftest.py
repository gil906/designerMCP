import json
import shutil
import uuid
from pathlib import Path

import pytest
from PIL import Image


@pytest.fixture
def root():
    # Keep test artifacts inside the workspace, never in system temporary dirs.
    parent = Path(__file__).resolve().parents[1] / ".test-work"
    path = parent / uuid.uuid4().hex
    (path / "data/assets").mkdir(parents=True)
    (path / "data/licenses").mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path)


@pytest.fixture
def entry():
    return {
        "id": "dashboard", "title": "Responsive dashboard",
        "summary": "A practical card grid for a web app.",
        "categories": ["layout", "responsive"], "tags": ["cards", "grid"],
        "platforms": ["web"], "guidance": "Use intrinsic columns and visible focus.",
        "source_url": "https://example.org/design", "license": "MIT",
        "license_path": "data/licenses/MIT.txt",
        "assets": [
            {"id": "preview", "path": "data/assets/preview.png", "mime_type": "image/png", "title": "Preview"},
            {"id": "motion", "path": "data/assets/motion.json", "mime_type": "application/json", "title": "Motion"},
            {"id": "vector", "path": "data/assets/vector.svg", "mime_type": "image/svg+xml", "title": "Vector"},
        ],
        "examples": [{"language": "css", "code": ".grid { display: grid; }", "description": "Unexecuted example"}],
    }


@pytest.fixture
def populated(root, entry):
    (root / "data/library.json").write_text(json.dumps({"version": 1, "entries": [entry]}))
    (root / "data/discovery.json").write_text(json.dumps({
        "sources": [
            {"id": "local", "status": "collected", "url": "https://example.org"},
            {"id": "remote", "status": "blocked", "reason": "License unavailable"},
        ]
    }))
    (root / "data/licenses/MIT.txt").write_text("Fixture license")
    (root / "data/assets/motion.json").write_text('{"v":"5.7","layers":[]}')
    (root / "data/assets/vector.svg").write_text('<svg onload="unsafe()"></svg>')
    Image.new("RGB", (2, 2), "blue").save(root / "data/assets/preview.png")
    return root
