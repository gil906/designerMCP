import hashlib
import json
import sys
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_offline_library import build, validate, inventory
from check_github_collection import CollectionError
from designer_mcp.library import Asset, Entry, Library, LibraryError, MAX_OUTPUT_BYTES


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def hash_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def offline(populated, entry):
    entry["assets"] = [a for a in entry["assets"] if a["id"] != "motion"]
    entry["examples"] = []
    write_json(populated / "data/library.json", {"version": 1, "entries": [entry]})
    (populated / "data/licenses/offline").mkdir()
    (populated / "data/licenses/offline/CC0.txt").write_text("CC0 fixture license")
    visual = {
        "id": "preview", "path": "assets/preview.png", "mime_type": "image/png",
        "title": "Original concept, not upstream screenshot", "origin": "original",
        "source_url": "original://designerMCP/concept/dashboard", "license": "CC0-1.0",
        "license_path": "licenses/offline/CC0.txt", "format": "png",
    }
    hashes = {
        "assets/preview.png": hash_file(populated / "data/assets/preview.png"),
        "licenses/offline/CC0.txt": hash_file(populated / "data/licenses/offline/CC0.txt"),
    }
    write_json(populated / "data/offline-preview-report.json", {
        "version": 1, "references": {"dashboard": {"assets": [visual], "kind": "reference-concept",
                                                   "limitations": ["Upstream screenshot unavailable."]}},
        "artifact_hashes": hashes, "limitations": [],
    })
    write_json(populated / "data/offline-source-report.json", {
        "version": 1, "references": {}, "artifact_hashes": {}, "limitations": [],
    })
    return populated


def test_wraps_full_text_retains_archive_provenance_and_is_idempotent(offline):
    original = offline / "data/assets/vector.svg"
    text = '<svg xmlns="http://www.w3.org/2000/svg"><text>' + ("á" * 18000) + '</text></svg>'
    original.write_text(text, encoding="utf-8")
    result = build(offline)
    assert result["reference_count"] == 1
    library = Library(offline)
    meta, raw = library.asset("dashboard", "vector")
    wrapper = json.loads(meta["animation_json"])
    assert raw is None and wrapper["text"] == text and wrapper["filename"] == "vector.svg"
    assert wrapper["sha256"] == hash_file(original) and wrapper["format"] == "svg"
    assert meta["provenance"]["license"] == "MIT"
    assert meta["asset"]["mime_type"] == "application/json"
    assert original.read_text() == text
    dossier = library.bundle("dashboard")["bundle"]
    assert dossier["coverage"]["visual"] == "original-concept"
    assert dossier["coverage"]["implementation"] == "unavailable"
    assert dossier["coverage"]["animation"] == "not-applicable"
    assert any("unavailable" in line for line in dossier["limitations"])
    before = {str(p.relative_to(offline)): p.read_bytes() for p in (offline / "data").rglob("*") if p.is_file()}
    assert build(offline) == result
    after = {str(p.relative_to(offline)): p.read_bytes() for p in (offline / "data").rglob("*") if p.is_file()}
    assert before == after
    assert library.status()["offline_coverage"]["bundle_count"] == 1


def test_original_asset_overrides_do_not_attribute_to_proprietary_entry(offline):
    build(offline)
    library = Library(offline)
    metadata, _ = library.asset("dashboard", "preview")
    assert metadata["provenance"]["origin"] == "original"
    assert metadata["provenance"]["source_url"] == "original://designerMCP/concept/dashboard"
    assert metadata["provenance"]["license"] == "CC0-1.0"
    assert metadata["provenance"]["license_path"] == "licenses/offline/CC0.txt"
    assert library.reference("dashboard")["provenance"]["license"] == "MIT"


@pytest.mark.parametrize("override", [
    {"source_url": "".join(("https://", "name", ":", "password", "@", "example.org/source"))},
    {"source_url": "file:///etc/passwd"},
    {"source_url": "https://example.org/\n"},
    {"license_path": "licenses/../../secret"},
    {"license_path": "/data/licenses/license.txt"},
    {"path": "assets/../../secret"},
    {"origin": "trusted"},
])
def test_unsafe_optional_provenance_rejected(entry, override):
    with pytest.raises((ValidationError, LibraryError)):
        Asset.model_validate({**entry["assets"][0], **override})


def test_original_origin_cannot_hide_upstream_attribution(entry):
    entry["assets"][0]["origin"] = "original"
    with pytest.raises(ValidationError):
        Entry.model_validate(entry)


@pytest.mark.parametrize("mutation", [
    lambda bundle: bundle.update(version=True),
    lambda bundle: bundle.update(reference_id="wrong"),
    lambda bundle: bundle.update(unknown="field"),
    lambda bundle: bundle["assets"][0].update(asset_id="absent"),
    lambda bundle: bundle["assets"][0].update(mime_type="text/html"),
    lambda bundle: bundle["assets"][0].update(source_url="".join(("https://", "user", ":", "secret", "@", "example.org"))),
    lambda bundle: bundle["assets"][0].update(license_path="licenses/../../secret"),
    lambda bundle: bundle.update(archives=[{"path": "../secret", "format": "text", "sha256": "0" * 64, "reason": "oversize"}]),
])
def test_bundle_corruption_rejected(offline, mutation):
    build(offline)
    library = Library(offline)
    asset = next(a for a in library.get("dashboard").assets if a.id == "offline-bundle")
    path = offline / "data" / asset.path
    value = json.loads(path.read_text())
    mutation(value)
    write_json(path, value)
    with pytest.raises(LibraryError, match="bundle"):
        library.bundle("dashboard")


def test_bundle_size_type_and_symlink_guards(offline):
    build(offline)
    library = Library(offline)
    asset = next(a for a in library.get("dashboard").assets if a.id == "offline-bundle")
    path = offline / "data" / asset.path
    for raw in (b"[]", b"{" + b" " * MAX_OUTPUT_BYTES + b"}"):
        path.write_bytes(raw)
        with pytest.raises(LibraryError):
            library.bundle("dashboard")
    path.unlink()
    path.symlink_to(offline / "data/assets/motion.json")
    with pytest.raises(LibraryError):
        library.bundle("dashboard")


def test_missing_upstream_has_explicit_original_guide_not_fake_code(offline):
    catalog = json.loads((offline / "data/library.json").read_text())
    catalog["entries"][0]["assets"] = [catalog["entries"][0]["assets"][0]]
    write_json(offline / "data/library.json", catalog)
    build(offline)
    library = Library(offline)
    dossier = library.bundle("dashboard")["bundle"]
    assert dossier["coverage"]["source"] == "original-guidance"
    assert dossier["coverage"]["implementation"] == "unavailable"
    metadata, _ = library.asset("dashboard", "offline-guidance")
    assert metadata["provenance"]["origin"] == "original"
    assert "no upstream implementation" in json.loads(metadata["animation_json"])["purpose"]


def test_real_animation_requires_gif_not_category(offline):
    catalog = json.loads((offline / "data/library.json").read_text())
    entry = catalog["entries"][0]
    entry["categories"].append("animation")
    write_json(offline / "data/library.json", catalog)
    build(offline)
    assert Library(offline).bundle("dashboard")["bundle"]["coverage"]["animation"] == "not-applicable"
    entry["assets"].append({"id": "motion", "path": "assets/motion.json", "mime_type": "application/json", "title": "Lottie"})
    write_json(offline / "data/library.json", catalog)
    with pytest.raises(CollectionError, match="GIF"):
        build(offline)
    Image.new("RGB", (3, 3), "red").save(offline / "data/assets/motion.gif", save_all=True,
                                        append_images=[Image.new("RGB", (3, 3), "blue")], duration=100, loop=0)
    report = json.loads((offline / "data/offline-preview-report.json").read_text())
    gif = {**report["references"]["dashboard"]["assets"][0], "id": "motion-gif", "path": "assets/motion.gif",
           "mime_type": "image/gif", "format": "gif"}
    report["references"]["dashboard"]["assets"].append(gif)
    report["references"]["dashboard"]["animation_source_asset_id"] = "motion"
    report["artifact_hashes"]["assets/motion.gif"] = hash_file(offline / "data/assets/motion.gif")
    write_json(offline / "data/offline-preview-report.json", report)
    result = build(offline)
    assert result["animated_reference_count"] == 1
    assert Library(offline).bundle("dashboard")["bundle"]["kind"] == "animated-icon"


def test_whole_inventory_scanned_even_for_original_or_unlisted_assets(offline):
    build(offline)
    (offline / "data/assets/extra.txt").write_text("unlisted")
    with pytest.raises(CollectionError, match="Unlisted"):
        validate(offline, min_references=0, min_animations=0, min_source_references=0)
    (offline / "data/assets/extra.txt").unlink()
    (offline / "data/assets/hostile.txt").write_text("eval('unsafe')")
    with pytest.raises(CollectionError, match="dynamic-execution"):
        build(offline)
    (offline / "data/assets/hostile.txt").unlink()
    (offline / "data/assets/disguised.png").write_text("eval('unsafe')")
    with pytest.raises(CollectionError, match="raster"):
        build(offline)


def test_report_hash_path_and_minimum_aggregate_guards(offline):
    with pytest.raises(CollectionError, match="minimum"):
        build(offline)
        validate(offline)
    report = json.loads((offline / "data/offline-preview-report.json").read_text())
    report["artifact_hashes"]["assets/preview.png"] = "0" * 64
    write_json(offline / "data/offline-preview-report.json", report)
    with pytest.raises(CollectionError, match="hash"):
        build(offline)
    report["artifact_hashes"] = {"../secret": "0" * 64}
    write_json(offline / "data/offline-preview-report.json", report)
    with pytest.raises((CollectionError, LibraryError)):
        build(offline)


def test_source_content_wrapper_and_oversize_original_are_honest(offline):
    report = json.loads((offline / "data/offline-source-report.json").read_text())
    path = offline / "data/assets/document.json"
    write_json(path, {"kind": "documentation", "content": "# Full selected document", "classification": "documentation"})
    asset = {
        "id": "document", "path": "assets/document.json", "mime_type": "application/json", "title": "Selected document",
        "origin": "upstream", "format": "plaintext", "license": "MIT",
        "license_path": "licenses/MIT.txt", "source_url": "https://example.org/document",
    }
    report["references"]["dashboard"] = {"assets": [asset], "limitations": []}
    report["artifact_hashes"] = {"assets/document.json": hash_file(path),
                                  "licenses/MIT.txt": hash_file(offline / "data/licenses/MIT.txt")}
    write_json(offline / "data/offline-source-report.json", report)
    catalog = json.loads((offline / "data/library.json").read_text())
    big = offline / "data/assets/large.svg"
    big.write_text("<svg>" + "x" * 140000 + "</svg>")
    catalog["entries"][0]["assets"].append({"id": "large", "path": "assets/large.svg", "mime_type": "image/svg+xml", "title": "Large"})
    write_json(offline / "data/library.json", catalog)
    build(offline)
    bundle = Library(offline).bundle("dashboard")["bundle"]
    assert bundle["coverage"]["source"] == "selected-files"
    assert bundle["coverage"]["implementation"] == "unavailable"
    assert any(item["path"] == "assets/large.svg" for item in bundle["archives"])
    assert all(item["asset_id"] != "large" for item in bundle["assets"])
    assert big.exists()
    first = (offline / "data/assets/offline-library/dashboard/offline-bundle.json").read_bytes()
    build(offline)
    assert (offline / "data/assets/offline-library/dashboard/offline-bundle.json").read_bytes() == first


def test_asset_cap_replaces_wrappers_instead_of_doubling_source_slots(offline):
    catalog = json.loads((offline / "data/library.json").read_text())
    entry = catalog["entries"][0]
    entry["assets"] = [entry["assets"][0]]
    for number in range(24):
        name = f"document-{number}.yaml"
        (offline / "data/assets" / name).write_text("name: selected-reference\n")
        entry["assets"].append({"id": f"doc-{number}", "path": "assets/" + name,
                                 "mime_type": "text/yaml", "title": "Stored document"})
    write_json(offline / "data/library.json", catalog)
    build(offline)
    assets = Library(offline).get("dashboard").assets
    assert len(assets) == 27  # 24 replaced files + preview/license/bundle.
    assert len([a for a in assets if a.id.startswith("doc-")]) == 24
    assert all(a.mime_type == "application/json" for a in assets if a.id.startswith("doc-"))


def test_static_gif_not_false_animated_success(offline):
    catalog = json.loads((offline / "data/library.json").read_text())
    catalog["entries"][0]["assets"].append({"id": "motion", "path": "assets/motion.json",
                                             "mime_type": "application/json", "title": "Expression-only Lottie"})
    write_json(offline / "data/library.json", catalog)
    report = json.loads((offline / "data/offline-preview-report.json").read_text())
    report["references"]["dashboard"]["kind"] = "unsupported-animation"
    report["references"]["dashboard"]["limitations"] = ["Source expressions are never executed; animated preview unavailable."]
    report["references"]["dashboard"]["animation_source_asset_id"] = "motion"
    write_json(offline / "data/offline-preview-report.json", report)
    result = build(offline)
    assert result["animation_source_reference_count"] == 1
    assert result["animated_reference_count"] == 0
    assert result["unsupported_animation_preview_count"] == 1
    assert Library(offline).bundle("dashboard")["bundle"]["coverage"]["animated_preview"] == "unavailable"
    with pytest.raises(CollectionError, match="minimum"):
        validate(offline, min_references=1, min_animations=1, min_source_references=0)


async def test_bundle_and_full_file_are_readonly_real_stdio(offline):
    import os
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    build(offline)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "designer_mcp.server", "--root", str(offline)],
        cwd=str(Path(__file__).resolve().parents[1]),
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            result = await client.call_tool("designer_get_bundle", {"id": "dashboard"})
            assert not result.isError
            bundle = result.structuredContent["bundle"]
            assert bundle["reference_id"] == "dashboard"
            assert bundle["coverage"]["implementation"] == "unavailable"
            file = await client.call_tool("designer_get_asset", {"reference_id": "dashboard", "asset_id": "vector"})
            assert [part.type for part in file.content] == ["text"]
            wrapper = json.loads(json.loads(file.content[0].text)["animation_json"])
            assert wrapper["text"] == (offline / "data/assets/vector.svg").read_text()
            unknown = await client.call_tool("designer_get_bundle", {"id": "../secret"})
            assert unknown.isError


def test_fixed_tooling_archive_is_detected_unserved_and_idempotent(offline, monkeypatch):
    import build_offline_library as builder

    path = "assets/build-tool.ts"
    raw = b'import { exec } from "node:child_process";\n'
    (offline / "data" / path).write_bytes(raw)
    monkeypatch.setattr(builder, "EXCLUDED_TOOLING", {path: hashlib.sha256(raw).hexdigest()})
    catalog = json.loads((offline / "data/library.json").read_text())
    catalog["entries"][0]["assets"].append({"id": "tool", "path": path, "mime_type": "text/typescript", "title": "Tool"})
    write_json(offline / "data/library.json", catalog)
    result = build(offline)
    assert result["unserved_tooling_archive_count"] == 1
    assert not (offline / "data" / path).exists()
    archived = offline / "data" / builder.excluded_path(path)
    assert archived.read_bytes() == raw
    metadata, _ = Library(offline).asset("dashboard", "tool")
    index = json.loads(metadata["animation_json"])
    assert index["complete"] is False and index["content_omitted"] is True
    assert "code" not in index and "text" not in index
    assert build(offline) == result
    archived.write_bytes(raw + b"eval(unsafe)")
    with pytest.raises(CollectionError, match="Unknown or changed"):
        builder.scan_artifact(builder.excluded_path(path), archived.read_bytes())


def test_arbitrary_archive_is_not_an_origin_marker_scan_bypass(offline):
    import build_offline_library as builder

    with pytest.raises(CollectionError, match="Unknown or changed"):
        builder.scan_artifact("archives/original-license/build.ts", b'import "node:child_process";')


@pytest.fixture
def relocated_import(offline, monkeypatch):
    import build_offline_library as builder
    import ingest_useanimations as ingest

    path = "assets/build-tool.ts"
    raw = b'import { exec } from "node:child_process";\n'
    (offline / "data" / path).write_bytes(raw)
    monkeypatch.setattr(builder, "EXCLUDED_TOOLING", {path: hashlib.sha256(raw).hexdigest()})
    catalog = json.loads((offline / "data/library.json").read_text())
    catalog["entries"][0]["assets"].append({"id": "tool", "path": path, "mime_type": "text/typescript", "title": "Tool"})
    write_json(offline / "data/library.json", catalog)
    write_json(offline / "data/import-report.json", {
        "assets": [{"path": path, "sha256": hashlib.sha256(raw).hexdigest(),
                    "git_blob_sha1": ingest.git_hash(raw), "upstream_path": "src/build-tool.ts"}],
        "original_assets": [], "downloaded_files": 1, "animation_count": 0, "downloaded_bytes": len(raw),
    })
    build(offline)
    monkeypatch.setattr(ingest, "DATA", offline / "data")
    monkeypatch.setattr(ingest.socket, "getaddrinfo", lambda *a, **k: pytest.fail("DNS used"))
    monkeypatch.setattr(ingest.Fetcher, "fetch", lambda *a, **k: pytest.fail("Network used"))
    return offline, ingest, builder.excluded_path(path)


def test_original_import_validator_uses_archived_full_bytes_offline(relocated_import):
    root, ingest, archive = relocated_import
    assert not (root / "data/assets/build-tool.ts").exists()
    assert ingest.validate() == 0
    report = json.loads((root / "data/import-report.json").read_text())
    report["assets"][0]["git_blob_sha1"] = "0" * 40
    write_json(root / "data/import-report.json", report)
    with pytest.raises(ValueError, match="checksum mismatch"):
        ingest.validate()


@pytest.mark.parametrize("mutation", ["missing", "path", "metadata", "hash", "restored"])
def test_original_import_archive_mapping_is_not_missing_file_skip(relocated_import, mutation):
    root, ingest, archive = relocated_import
    bundle_path = root / "data/assets/offline-library/dashboard/offline-bundle.json"
    bundle = json.loads(bundle_path.read_text())
    if mutation == "missing":
        (root / "data" / archive).unlink()
    elif mutation == "restored":
        (root / "data/assets/build-tool.ts").write_bytes((root / "data" / archive).read_bytes())
    elif mutation == "path":
        bundle["archives"][0]["path"] = "archives/offline-tooling/other-tool.ts"
    elif mutation == "metadata":
        bundle["archives"][0]["original_asset"] = None
    else:
        bundle["archives"][0]["sha256"] = "0" * 64
    write_json(bundle_path, bundle)
    with pytest.raises((ValueError, LibraryError)):
        ingest.validate()
